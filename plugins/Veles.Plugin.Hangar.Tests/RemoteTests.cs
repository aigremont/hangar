using NUnit.Framework;
using Veles.Plugin.Hangar.Core;

namespace Veles.Plugin.Hangar.Tests;

public class LineLogTests
{
    [Test]
    public void Since_returns_only_newer_lines_in_order()
    {
        var log = new LineLog(10);
        log.Add("a"); log.Add("b"); log.Add("c");
        var (lines, next, truncated) = log.Since(1);
        Assert.That(lines.ConvertAll(l => l.Text), Is.EqualTo(new[] { "b", "c" }));
        Assert.That(lines.ConvertAll(l => l.Seq), Is.EqualTo(new long[] { 2, 3 }));
        Assert.That(next, Is.EqualTo(3));
        Assert.That(truncated, Is.False);
    }

    [Test]
    public void Since_zero_returns_everything_and_polling_from_next_returns_nothing()
    {
        var log = new LineLog(10);
        log.Add("a"); log.Add("b");
        var (all, next, _) = log.Since(0);
        Assert.That(all, Has.Count.EqualTo(2));
        Assert.That(log.Since(next).Lines, Is.Empty);
    }

    [Test]
    public void Ring_keeps_only_the_newest_and_flags_truncation()
    {
        var log = new LineLog(3);
        for (int i = 1; i <= 7; i++) log.Add("l" + i);
        var (lines, next, truncated) = log.Since(0);
        Assert.That(lines.ConvertAll(l => l.Text), Is.EqualTo(new[] { "l5", "l6", "l7" }));
        Assert.That(lines[0].Seq, Is.EqualTo(5));
        Assert.That(next, Is.EqualTo(7));
        Assert.That(truncated, Is.True);
        Assert.That(log.Since(4).Truncated, Is.False);   // line 5 is the oldest kept: nothing missed
        Assert.That(log.Since(3).Truncated, Is.True);    // line 4 was lost
    }

    [Test]
    public void Empty_log_and_caller_ahead_of_us()
    {
        var log = new LineLog(5);
        Assert.That(log.Since(0).Lines, Is.Empty);
        log.Add("x");
        var (lines, next, _) = log.Since(99);
        Assert.That(lines, Is.Empty);
        Assert.That(next, Is.EqualTo(1));
    }

    [Test]
    public void Multi_line_text_becomes_one_entry_per_line()
    {
        var log = new LineLog(10);
        Assert.That(log.Add("one\r\ntwo"), Is.EqualTo(2));
        Assert.That(log.Since(0).Lines.ConvertAll(l => l.Text), Is.EqualTo(new[] { "one", "two" }));
    }
}

public class RemotePolicyTests
{
    const string Token = "0123456789abcdef0123456789abcdef";

    [TestCase("Bearer 0123456789abcdef0123456789abcdef", true)]
    [TestCase("Bearer 0123456789abcdef0123456789abcdee", false)]
    [TestCase("Bearer ", false)]
    [TestCase("bearer 0123456789abcdef0123456789abcdef", false)]
    [TestCase("0123456789abcdef0123456789abcdef", false)]
    [TestCase(null, false)]
    public void Authorized_needs_the_exact_bearer_token(string? header, bool expected) =>
        Assert.That(RemotePolicy.Authorized(header, Token), Is.EqualTo(expected));

    [Test]
    public void No_token_set_authorizes_nobody() =>
        Assert.That(RemotePolicy.Authorized("Bearer ", ""), Is.False);

    [TestCase(null, 48213)]
    [TestCase("", 48213)]
    [TestCase("abc", 48213)]
    [TestCase("80", 48213)]
    [TestCase("70000", 48213)]
    [TestCase("50000", 50000)]
    public void ParsePort_falls_back_to_the_default(string? env, int expected) =>
        Assert.That(RemotePolicy.ParsePort(env), Is.EqualTo(expected));

    [TestCase("Jane Example", "Jane_Example")]
    [TestCase("Alex Sample", "Alex_Sample")]
    [TestCase("Some.Name resident", "Some_Name_resident")]
    [TestCase("  Padded Name ", "Padded_Name")]
    [TestCase("", "unknown")]
    [TestCase(null, "unknown")]
    public void Slug_replaces_non_alphanumerics(string? avatar, string expected) =>
        Assert.That(RemotePolicy.Slug(avatar), Is.EqualTo(expected));

    [Test]
    public void Token_file_name_and_pattern()
    {
        Assert.That(RemotePolicy.TokenFileName("Jane Example"), Is.EqualTo("remote-Jane_Example.json"));
        Assert.That(RemotePolicy.IsTokenFileName("remote-Alex_Sample.json"), Is.True);
        Assert.That(RemotePolicy.IsTokenFileName("remote.json"), Is.False);
    }

    [Test]
    public void Candidate_ports_are_ten_upwards_and_stop_at_65535()
    {
        Assert.That(RemotePolicy.CandidatePorts(48213), Is.EqualTo(System.Linq.Enumerable.Range(48213, 10)));
        Assert.That(RemotePolicy.CandidatePorts(65533), Is.EqualTo(new[] { 65533, 65534, 65535 }));
    }

    [Test]
    public void Token_file_is_stale_when_its_pid_is_not_running()
    {
        Assert.That(RemotePolicy.IsStale("{\"port\":1,\"pid\":10}", pid => pid == 10), Is.False);
        Assert.That(RemotePolicy.IsStale("{\"port\":1,\"pid\":11}", pid => pid == 10), Is.True);
        Assert.That(RemotePolicy.IsStale("{\"port\":1}", _ => true), Is.True, "no pid");
        Assert.That(RemotePolicy.IsStale("garbage", _ => true), Is.True);
    }

    [Test]
    public void CleanStale_deletes_only_dead_remote_files()
    {
        var dir = System.IO.Path.Combine(System.IO.Path.GetTempPath(), "hangar-stale-" + System.Guid.NewGuid().ToString("N"));
        System.IO.Directory.CreateDirectory(dir);
        try
        {
            System.IO.File.WriteAllText(System.IO.Path.Combine(dir, "remote-Alive_One.json"), "{\"pid\":1}");
            System.IO.File.WriteAllText(System.IO.Path.Combine(dir, "remote-Dead_One.json"), "{\"pid\":2}");
            System.IO.File.WriteAllText(System.IO.Path.Combine(dir, "other.json"), "{\"pid\":2}");
            Veles.Plugin.Hangar.Live.RemoteControl.CleanStale(dir, pid => pid == 1);
            Assert.That(System.IO.File.Exists(System.IO.Path.Combine(dir, "remote-Alive_One.json")), Is.True);
            Assert.That(System.IO.File.Exists(System.IO.Path.Combine(dir, "remote-Dead_One.json")), Is.False);
            Assert.That(System.IO.File.Exists(System.IO.Path.Combine(dir, "other.json")), Is.True);
        }
        finally { System.IO.Directory.Delete(dir, true); }
    }

    [Test]
    public void Tokenize_splits_on_spaces_groups_quotes_and_keeps_backslashes()
    {
        Assert.That(RemotePolicy.Tokenize("apply 5"), Is.EqualTo(new[] { "apply", "5" }));
        Assert.That(RemotePolicy.Tokenize(@"  load   ""W:\my dir\plan.csv"" "), Is.EqualTo(new[] { "load", @"W:\my dir\plan.csv" }));
        Assert.That(RemotePolicy.Tokenize(""), Is.Empty);
    }

    static readonly RemoteState Idle = new(Busy: false, ChatSwitchOn: true);

    [TestCase("check")]
    [TestCase("fetch")]
    [TestCase("apply 5")]
    [TestCase("undo")]
    [TestCase("remote off")]
    [TestCase("remote status")]
    [TestCase("load W:/x/plan.csv")]
    public void Idle_with_switch_on_allows(string text) =>
        Assert.That(RemotePolicy.Check(RemotePolicy.Tokenize(text), Idle).Allowed, Is.True);

    [Test]
    public void Remote_on_is_never_allowed_over_http() =>
        Assert.That(RemotePolicy.Check(new[] { "remote", "on" }, Idle).Status, Is.EqualTo(403));

    [TestCase("apply")]
    [TestCase("undo")]
    [TestCase("remote off")]
    public void Dangerous_commands_need_the_chat_switch(string text)
    {
        var v = RemotePolicy.Check(RemotePolicy.Tokenize(text), new RemoteState(false, false));
        Assert.That(v.Status, Is.EqualTo(403));
        Assert.That(v.Error, Is.Not.Empty);
    }

    [Test]
    public void Harmless_commands_do_not_need_the_chat_switch() =>
        Assert.That(RemotePolicy.Check(new[] { "check" }, new RemoteState(false, false)).Allowed, Is.True);

    [TestCase("check")]
    [TestCase("fetch")]
    [TestCase("status")]
    [TestCase("apply")]
    public void Busy_refuses_everything_but_stop(string text) =>
        Assert.That(RemotePolicy.Check(RemotePolicy.Tokenize(text), new RemoteState(true, true)).Status, Is.EqualTo(403));

    [Test]
    public void Stop_is_allowed_while_busy() =>
        Assert.That(RemotePolicy.Check(new[] { "stop" }, new RemoteState(true, true)).Allowed, Is.True);

    [Test]
    public void Empty_command_and_pathless_load_are_bad_requests()
    {
        Assert.That(RemotePolicy.Check(new string[0], Idle).Status, Is.EqualTo(400));
        Assert.That(RemotePolicy.Check(new[] { "load" }, Idle).Status, Is.EqualTo(400));
        Assert.That(RemotePolicy.Check(new[] { "remote", "bogus" }, Idle).Status, Is.EqualTo(400));
    }

    [TestCase("fetch", true)]
    [TestCase("APPLY", true)]
    [TestCase("verify", true)]
    [TestCase("undo", true)]
    [TestCase("export", true)]
    [TestCase("check", false)]
    [TestCase("status", false)]
    public void Long_running_verbs(string verb, bool expected) =>
        Assert.That(RemotePolicy.IsLongRunning(verb), Is.EqualTo(expected));
}

public class RemoteControlHttpTests
{
    sealed class StubHost : Veles.Plugin.Hangar.Live.IRemoteHost
    {
        public LineLog Log = new();
        public bool Busy, Switch = true;
        public Veles.Plugin.Hangar.Core.RemoteState State => new(Busy, Switch);
        public object Status() => new { avatar = "Test Avatar", loaded = (string?)null, fetched = false, busy = Busy };
        public Veles.Plugin.Hangar.Live.RemoteRun Run(string text, string[] args)
        {
            long start = Log.LastSeq;
            Log.Add("ran " + text);
            var (lines, next, _) = Log.Since(start);
            return new(RemotePolicy.IsLongRunning(args[0]), lines.ConvertAll(l => l.Text), next, args[0] == "remote");
        }
    }

    [Test]
    public async System.Threading.Tasks.Task Token_guards_every_path_and_commands_round_trip()
    {
        var file = System.IO.Path.Combine(System.IO.Path.GetTempPath(), "hangar-test-" + System.Guid.NewGuid().ToString("N"), "remote.json");
        var host = new StubHost();
        using var remote = new Veles.Plugin.Hangar.Live.RemoteControl(host.Log, host, "Test Avatar", file);
        int port = 49000 + System.Random.Shared.Next(900);
        remote.Start(port);
        try
        {
            using var doc = System.Text.Json.JsonDocument.Parse(System.IO.File.ReadAllText(file));
            var token = doc.RootElement.GetProperty("token").GetString()!;
            Assert.That(token, Has.Length.EqualTo(32));
            Assert.That(doc.RootElement.GetProperty("port").GetInt32(), Is.EqualTo(port));
            Assert.That(doc.RootElement.GetProperty("pid").GetInt32(), Is.EqualTo(System.Environment.ProcessId));
            Assert.That(doc.RootElement.GetProperty("avatar").GetString(), Is.EqualTo("Test Avatar"));

            // A second instance asked for the same port takes the next one.
            var file2 = System.IO.Path.Combine(System.IO.Path.GetDirectoryName(file)!, "remote-second.json");
            using var second = new Veles.Plugin.Hangar.Live.RemoteControl(new LineLog(), host, "Second", file2);
            second.Start(port);
            Assert.That(second.Port, Is.EqualTo(port + 1));
            second.Stop();
            Assert.That(System.IO.File.Exists(file2), Is.False);
            Assert.That(System.IO.File.Exists(file), Is.True, "stopping one leaves the other's file");

            using var http = new System.Net.Http.HttpClient { BaseAddress = new System.Uri($"http://127.0.0.1:{port}/") };
            Assert.That((await http.GetAsync("status")).StatusCode, Is.EqualTo(System.Net.HttpStatusCode.Unauthorized));
            http.DefaultRequestHeaders.Authorization = new("Bearer", "wrong");
            Assert.That((await http.GetAsync("lines")).StatusCode, Is.EqualTo(System.Net.HttpStatusCode.Unauthorized));
            http.DefaultRequestHeaders.Authorization = new("Bearer", token);

            Assert.That(await http.GetStringAsync("status"), Does.Contain("Test Avatar"));
            Assert.That((await http.GetAsync("nope")).StatusCode, Is.EqualTo(System.Net.HttpStatusCode.NotFound));

            static System.Net.Http.StringContent Body(string t) =>
                new(System.Text.Json.JsonSerializer.Serialize(new { text = t }), System.Text.Encoding.UTF8, "application/json");

            var r = await http.PostAsync("command", Body("check"));
            var text = await r.Content.ReadAsStringAsync();
            Assert.That(r.StatusCode, Is.EqualTo(System.Net.HttpStatusCode.OK));
            Assert.That(text, Does.Contain("\"started\":false").And.Contain("ran check"));

            Assert.That(await (await http.PostAsync("command", Body("fetch"))).Content.ReadAsStringAsync(), Does.Contain("\"started\":true"));
            Assert.That(await http.GetStringAsync("lines?since=0"), Does.Contain("ran fetch"));

            Assert.That((await http.PostAsync("command", Body("remote on"))).StatusCode, Is.EqualTo(System.Net.HttpStatusCode.Forbidden));
            host.Busy = true;
            Assert.That((await http.PostAsync("command", Body("check"))).StatusCode, Is.EqualTo(System.Net.HttpStatusCode.Forbidden));
            host.Busy = false;
            Assert.That((await http.PostAsync("command", new System.Net.Http.StringContent("not json"))).StatusCode, Is.EqualTo(System.Net.HttpStatusCode.BadRequest));

            // "remote off" stops the listener once the reply is out and removes the token file.
            Assert.That((await http.PostAsync("command", Body("remote off"))).StatusCode, Is.EqualTo(System.Net.HttpStatusCode.OK));
            for (int i = 0; i < 50 && (remote.IsOn || System.IO.File.Exists(file)); i++) await System.Threading.Tasks.Task.Delay(50);
            Assert.That(remote.IsOn, Is.False);
            Assert.That(System.IO.File.Exists(file), Is.False);
        }
        finally { remote.Stop(); }
    }
}
