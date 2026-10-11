using System;
using System.Collections.Generic;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace Veles.Plugin.Hangar.Core;

/// <summary>What the remote-control policy needs to know about the plugin right now.</summary>
public readonly record struct RemoteState(bool Busy, bool ChatSwitchOn);

/// <summary>The verdict on a remote request: an HTTP status and, if refused, why.</summary>
public readonly record struct RemoteVerdict(int Status, string? Error)
{
    public bool Allowed => Status == 200;
    public static RemoteVerdict Ok => new(200, null);
}

/// <summary>Pure rules for the local control channel: who may call, which commands may run, and how
/// the command text is split. No network, no Veles.</summary>
public static class RemotePolicy
{
    public const int DefaultPort = 48213;

    /// <summary>Commands that run in the background; the HTTP call returns at once and the caller polls the lines.</summary>
    static readonly HashSet<string> Long = new() { "fetch", "apply", "verify", "undo", "export" };

    /// <summary>Commands that change the inventory (or the remote switch itself): HTTP may issue them only
    /// while the user has turned remote control on from chat in this login session.</summary>
    static readonly HashSet<string> NeedsSwitch = new() { "apply", "undo" };

    public static bool IsLongRunning(string verb) => Long.Contains(verb.ToLowerInvariant());

    /// <summary>True if the Authorization header is exactly "Bearer &lt;token&gt;" (constant-time comparison).</summary>
    public static bool Authorized(string? authorizationHeader, string token)
    {
        if (string.IsNullOrEmpty(token) || authorizationHeader == null) return false;
        const string prefix = "Bearer ";
        if (!authorizationHeader.StartsWith(prefix, StringComparison.Ordinal)) return false;
        var given = Encoding.UTF8.GetBytes(authorizationHeader.Substring(prefix.Length).Trim());
        var want = Encoding.UTF8.GetBytes(token);
        return CryptographicOperations.FixedTimeEquals(given, want);
    }

    /// <summary>Port from the HANGAR_REMOTE_PORT value, or the default if it is missing or not a usable port.</summary>
    public static int ParsePort(string? env) =>
        int.TryParse(env, out var p) && p is >= 1024 and <= 65535 ? p : DefaultPort;

    /// <summary>How many ports, starting at the wanted one, are tried before giving up.</summary>
    public const int PortTries = 10;

    /// <summary>The ports to try in order: the wanted one, then the next nine (never past 65535).</summary>
    public static IEnumerable<int> CandidatePorts(int first) =>
        Enumerable.Range(first, PortTries).Where(p => p <= 65535);

    /// <summary>The avatar name with every character that is not a letter or digit replaced by '_'
    /// ("Jane Example" becomes "Jane_Example"); "unknown" if there is no name.</summary>
    public static string Slug(string? avatar)
    {
        if (string.IsNullOrWhiteSpace(avatar)) return "unknown";
        var sb = new StringBuilder();
        foreach (var c in avatar.Trim()) sb.Append(c is (>= 'a' and <= 'z') or (>= 'A' and <= 'Z') or (>= '0' and <= '9') ? c : '_');
        return sb.ToString();
    }

    /// <summary>The token file name for an avatar: remote-&lt;slug&gt;.json.</summary>
    public static string TokenFileName(string? avatar) => "remote-" + Slug(avatar) + ".json";

    /// <summary>True for a file name that is a remote token file.</summary>
    public static bool IsTokenFileName(string name) =>
        name.StartsWith("remote-", StringComparison.OrdinalIgnoreCase) && name.EndsWith(".json", StringComparison.OrdinalIgnoreCase);

    /// <summary>True if a token file's content names a process that is no longer running (left by a crash).
    /// A file with no readable pid is stale too: this plugin always writes one.</summary>
    public static bool IsStale(string? json, Func<int, bool> isRunning)
    {
        try
        {
            using var doc = JsonDocument.Parse(json ?? "");
            if (doc.RootElement.TryGetProperty("pid", out var pid) && pid.TryGetInt32(out var id)) return !isRunning(id);
        }
        catch (JsonException) { }
        return true;
    }

    /// <summary>Splits the text after "//hangar" into arguments: whitespace separates, double quotes group,
    /// and backslashes are kept as they are (Windows paths).</summary>
    public static string[] Tokenize(string text)
    {
        var args = new List<string>();
        var cur = new StringBuilder();
        bool inQuotes = false, any = false;
        foreach (var c in text ?? "")
        {
            if (c == '"') { inQuotes = !inQuotes; any = true; }
            else if (!inQuotes && char.IsWhiteSpace(c))
            {
                if (any || cur.Length > 0) args.Add(cur.ToString());
                cur.Clear();
                any = false;
            }
            else cur.Append(c);
        }
        if (any || cur.Length > 0) args.Add(cur.ToString());
        return args.ToArray();
    }

    /// <summary>May this command, arriving over HTTP, run now? (Whatever the chat path refuses is refused
    /// by the command itself; this is only what is stricter over HTTP.)</summary>
    public static RemoteVerdict Check(string[] args, RemoteState state)
    {
        if (args.Length == 0) return new(400, "empty command");
        var verb = args[0].ToLowerInvariant();

        if (verb == "remote")
        {
            var sub = args.Length > 1 ? args[1].ToLowerInvariant() : "status";
            if (sub == "on") return new(403, "remote on can only be typed in Veles chat");
            if (sub == "off" && !state.ChatSwitchOn) return new(403, "remote off over HTTP needs remote on from chat in this login session");
            if (sub != "off" && sub != "status") return new(400, "usage: remote on|off|status");
        }
        if (verb == "load" && args.Length < 2) return new(400, "load needs a path over HTTP (no file picker)");
        if (NeedsSwitch.Contains(verb) && !state.ChatSwitchOn)
            return new(403, verb + " over HTTP needs remote on from chat in this login session");
        // Stopping the running step is the one thing allowed while busy.
        if (state.Busy && verb != "stop") return new(403, "busy: a step is running");
        return RemoteVerdict.Ok;
    }
}
