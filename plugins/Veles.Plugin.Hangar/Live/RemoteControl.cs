/*
 * A local control channel: a loopback-only HTTP listener so an assistant on the same machine can
 * run Hangar commands and read what they said, without typing in the Veles chat box.
 *
 * Off at every login; the user turns it on with "//hangar remote on". Every request must carry the
 * bearer token written to %LOCALAPPDATA%\Hangar\remote-<avatar>.json (one file per avatar, so two
 * Veles instances can be on at once, each on its own port). The rules live in Core/RemotePolicy.cs.
 */

using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using Veles.Plugin.Hangar.Core;

namespace Veles.Plugin.Hangar.Live;

/// <summary>What a remote command did: whether it started a background step, the lines it said so far,
/// the sequence number to poll from, and whether the command asked to switch remote control off.</summary>
public sealed record RemoteRun(bool Started, List<string> Lines, long Next, bool StopRemote);

/// <summary>The plugin side of the channel.</summary>
public interface IRemoteHost
{
    RemoteState State { get; }
    object Status();
    RemoteRun Run(string text, string[] args);
}

public sealed class RemoteControl : IDisposable
{
    readonly LineLog _log;
    readonly IRemoteHost _host;
    readonly string _avatar;
    readonly string _tokenFile;
    readonly object _commandLock = new();
    HttpListener? _listener;
    string _token = "";

    public RemoteControl(LineLog log, IRemoteHost host, string avatar, string? tokenFile = null)
    {
        _log = log;
        _host = host;
        _avatar = avatar;
        _tokenFile = tokenFile ?? DefaultTokenFile(avatar);
    }

    public static string TokenDirectory() => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Hangar");

    public static string DefaultTokenFile(string avatar) => Path.Combine(TokenDirectory(), RemotePolicy.TokenFileName(avatar));

    public string TokenFile => _tokenFile;

    /// <summary>Deletes remote-*.json files whose pid is not a running process (left by a crash).
    /// Files of running processes, ours or another Veles instance's, are never touched.</summary>
    public static void CleanStale(string? directory = null, Func<int, bool>? isRunning = null)
    {
        isRunning ??= pid =>
        {
            try { using var p = System.Diagnostics.Process.GetProcessById(pid); return !p.HasExited; }
            catch { return false; }
        };
        try
        {
            var dir = directory ?? TokenDirectory();
            if (!Directory.Exists(dir)) return;
            foreach (var f in Directory.GetFiles(dir, "remote-*.json"))
            {
                try
                {
                    if (RemotePolicy.IsStale(File.ReadAllText(f), isRunning)) File.Delete(f);
                }
                catch { /* unreadable or locked: leave it */ }
            }
        }
        catch { /* best effort */ }
    }

    public bool IsOn => _listener is { IsListening: true };
    public int Port { get; private set; }

    /// <summary>Starts listening on 127.0.0.1 on the first free port from <paramref name="port"/> up
    /// (nine more are tried) and writes the token file. Throws if none is free; <see cref="Port"/> is the one chosen.</summary>
    public void Start(int port)
    {
        if (IsOn) return;
        HttpListener? listener = null;
        Exception? last = null;
        foreach (var candidate in RemotePolicy.CandidatePorts(port))
        {
            var l = new HttpListener();
            l.Prefixes.Add($"http://127.0.0.1:{candidate}/");
            try { l.Start(); listener = l; port = candidate; break; }   // HttpListenerException if the port is in use
            catch (HttpListenerException ex) { last = ex; try { l.Close(); } catch { } }
        }
        if (listener == null) throw last ?? new InvalidOperationException("no port available");
        _listener = listener;
        Port = port;
        _token = Convert.ToHexString(RandomNumberGenerator.GetBytes(16)).ToLowerInvariant();
        Directory.CreateDirectory(Path.GetDirectoryName(_tokenFile)!);
        var tmp = _tokenFile + ".tmp";
        File.WriteAllText(tmp, JsonSerializer.Serialize(new { port, token = _token, avatar = _avatar, pid = Environment.ProcessId }));
        File.Move(tmp, _tokenFile, true);
        _ = Task.Run(() => Loop(listener));
    }

    /// <summary>Stops listening and deletes the token file (if it is still ours).</summary>
    public void Stop()
    {
        var listener = _listener;
        _listener = null;
        try { listener?.Close(); } catch { /* already closed */ }
        try
        {
            if (_token.Length > 0 && File.Exists(_tokenFile) && File.ReadAllText(_tokenFile).Contains(_token))
                File.Delete(_tokenFile);
        }
        catch { /* a file we cannot remove is stale at worst; the token is dead with the listener */ }
        _token = "";
    }

    public void Dispose() => Stop();

    async Task Loop(HttpListener listener)
    {
        while (listener.IsListening)
        {
            HttpListenerContext ctx;
            try { ctx = await listener.GetContextAsync().ConfigureAwait(false); }
            catch { return; }   // listener closed
            _ = Task.Run(() => Handle(ctx));
        }
    }

    void Handle(HttpListenerContext ctx)
    {
        bool stopAfter = false;
        try
        {
            var req = ctx.Request;
            // Browsers send Origin; a web page has no business driving this.
            if (req.Headers["Origin"] != null) { Send(ctx, 403, Error("browser requests are refused")); return; }
            if (!RemotePolicy.Authorized(req.Headers["Authorization"], _token))
            {
                ctx.Response.AddHeader("WWW-Authenticate", "Bearer");
                Send(ctx, 401, Error("missing or wrong token"));
                return;
            }
            var path = req.Url?.AbsolutePath ?? "/";
            switch (path)
            {
                case "/status" when req.HttpMethod == "GET":
                    Send(ctx, 200, _host.Status());
                    return;
                case "/lines" when req.HttpMethod == "GET":
                {
                    long since = long.TryParse(req.QueryString["since"], out var s) ? s : 0;
                    var (lines, next, truncated) = _log.Since(since);
                    Send(ctx, 200, new
                    {
                        ok = true,
                        lines = lines.ConvertAll(l => new { seq = l.Seq, text = l.Text }),
                        next,
                        truncated,
                        busy = _host.State.Busy,
                    });
                    return;
                }
                case "/command" when req.HttpMethod == "POST":
                    stopAfter = HandleCommand(ctx);
                    return;
                case "/status" or "/lines" or "/command":
                    Send(ctx, 405, Error("wrong method"));
                    return;
                default:
                    Send(ctx, 404, Error("unknown path"));
                    return;
            }
        }
        catch (Exception ex)
        {
            try { Send(ctx, 500, Error("internal error: " + ex.Message)); } catch { }
        }
        finally
        {
            if (stopAfter) Stop();
        }
    }

    /// <summary>Returns true if remote control is to be switched off once the reply is sent.</summary>
    bool HandleCommand(HttpListenerContext ctx)
    {
        string text;
        try
        {
            using var reader = new StreamReader(ctx.Request.InputStream, Encoding.UTF8);
            var buf = new char[16 * 1024 + 1];
            int n = reader.ReadBlock(buf, 0, buf.Length);
            if (n > 16 * 1024) { Send(ctx, 413, Error("body too large")); return false; }
            using var doc = JsonDocument.Parse(new string(buf, 0, n));
            text = doc.RootElement.GetProperty("text").GetString() ?? "";
        }
        catch (Exception)
        {
            Send(ctx, 400, Error("body must be JSON like {\"text\": \"check\"}"));
            return false;
        }
        text = text.Trim();
        if (text.StartsWith("//hangar", StringComparison.OrdinalIgnoreCase)) text = text.Substring(8).Trim();
        var args = RemotePolicy.Tokenize(text);

        // One command at a time: a second caller while one is being started is "busy" too.
        if (!Monitor.TryEnter(_commandLock))
        {
            Send(ctx, 403, Error("busy: a step is running"));
            return false;
        }
        try
        {
            var verdict = RemotePolicy.Check(args, _host.State);
            if (!verdict.Allowed) { Send(ctx, verdict.Status, Error(verdict.Error!)); return false; }
            var run = _host.Run(text, args);
            Send(ctx, 200, new { ok = true, started = run.Started, lines = run.Lines, next = run.Next });
            return run.StopRemote;
        }
        finally { Monitor.Exit(_commandLock); }
    }

    static object Error(string message) => new { ok = false, error = message };

    static void Send(HttpListenerContext ctx, int status, object body)
    {
        var bytes = JsonSerializer.SerializeToUtf8Bytes(body);
        var res = ctx.Response;
        res.StatusCode = status;
        res.ContentType = "application/json; charset=utf-8";
        res.ContentLength64 = bytes.Length;
        res.OutputStream.Write(bytes, 0, bytes.Length);
        res.Close();
    }
}
