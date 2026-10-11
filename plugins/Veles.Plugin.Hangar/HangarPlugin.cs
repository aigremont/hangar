/*
 * Hangar: applies an inventory plan made by tools/inventory/inventory_triage.py.
 *
 * Nothing moves until the user has loaded a plan, fetched the inventory live,
 * checked the plan against it, read the preview, and typed "//hangar apply".
 * Every change is journalled before it is sent, and "//hangar undo" reverses it.
 */

using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.ApplicationLifetimes;
using Avalonia.Platform.Storage;
using Avalonia.Threading;
using LibreMetaverse;
using Radegast.Veles.PluginApi;
using Veles.Plugin.Hangar.Core;
using Veles.Plugin.Hangar.Live;

namespace Veles.Plugin.Hangar;

[VelesPlugin("Hangar",
    Description = "Applies an inventory sorting plan: checks it against the live inventory, previews it, moves folders in batches with an undo journal.",
    Author = "aigremont",
    Version = "0.1.0")]
public sealed class HangarPlugin : IVelesPlugin, IRemoteHost
{
    const string Usage =
        "//hangar load [W:/path/plan.csv] | fetch | check | apply [N] | verify | undo [journal.tsv] | export | remote on|off|status | status | stop  (type the // or it is sent as public chat)";

    IPluginContext _ctx = null!;
    string? _planPath;
    LoadedPlan? _plan;
    bool _fetched;
    int _unanswered;               // folders the latest fetch got no answer for
    HashSet<string>? _confirmed;   // folder ids the server returned in the latest fetch
    ItemIndex? _items;             // the items the server listed in the latest fetch, kept current by apply and undo
    ICheckedPlan? _checked;
    string? _lastJournal;
    List<(UUID Folder, UUID From, UUID To, string Name)>? _lastMoves;
    List<(UUID Item, UUID From, UUID To, string Name)>? _lastItemMoves;
    List<(UUID Folder, UUID Parent, string OldName, string NewName)>? _lastRenames;
    CancellationTokenSource? _cts;
    Task? _running;
    int _startCount;               // background steps started so far (the remote channel tells "started" from "refused")
    readonly LineLog _log = new();  // everything Hangar said, for the remote channel's /lines
    RemoteControl? _remote;
    bool _chatSwitch;              // "//hangar remote on" was typed in chat in this login session
    bool _fromRemote;              // the command now running came over HTTP
    bool _stopRemoteAfter;         // "remote off" came over HTTP: stop once the reply is sent

    public void Attach(IPluginContext context)
    {
        _ctx = context;
        _ctx.RegisterCommand("hangar", "Apply an inventory sorting plan", Usage,
            (args, writeLine) => OnCommand(args, line => { _log.Add(line); writeLine(line); }));
        _ctx.AddMenuItem(new PluginMenuItemInfo("hangar_load", "Hangar: _Load plan…", OnLoadMenu));
        _ctx.Disconnected += OnDisconnected;
        _lastJournal = _ctx.GetSetting("last_journal");
        // Remote control is off at every login; a token file left by a crashed instance is dead and
        // misleading. Files of instances still running (another avatar) are left alone.
        RemoteControl.CleanStale();
    }

    public void Detach()
    {
        _cts?.Cancel();
        StopRemote();
        _ctx.Disconnected -= OnDisconnected;
    }

    public void Dispose() => _cts?.Dispose();

    void Say(string message)
    {
        _log.Add(message);
        _ctx.LogToChat("[Hangar] " + message);
    }

    void OnDisconnected(object? sender, EventArgs e)
    {
        StopRemote();
        if (_running is { IsCompleted: false })
        {
            _cts?.Cancel();
            Say("disconnected: stopped. Log in again, then fetch and check before applying.");
        }
        _fetched = false;
        _confirmed = null;
        _items = null;
        _checked = null;
    }

    async void OnLoadMenu()
    {
        try
        {
            // Commands run on Radegast's command thread; pickers must open on the UI thread.
            var path = await Dispatcher.UIThread.InvokeAsync(PickPlanAsync);
            if (path == null)
            {
                Say("no plan chosen. You can also type the path with forward slashes: //hangar load W:/folder/plan.csv");
                return;
            }
            Load(path, Say);
        }
        catch (Exception ex) { Say("could not open the plan: " + ex.Message); }
    }

    async Task<string?> PickPlanAsync()
    {
        // IPluginContext.OpenFilePickerAsync asks the app lifetime's MainWindow, which
        // Veles never sets after login, so it silently returns nothing. Use the
        // Veles window that is actually open instead.
        var top = FindWindow();
        if (top == null) return null;
        var options = new FilePickerOpenOptions
        {
            Title = "Hangar: choose plan.csv",
            AllowMultiple = false,
            FileTypeFilter = new[] { new FilePickerFileType("Plan") { Patterns = new[] { "*.csv" } } },
        };
        var last = _ctx.GetSetting("last_plan");
        if (last != null && Path.GetDirectoryName(last) is { } dir && Directory.Exists(dir))
            options.SuggestedStartLocation = await top.StorageProvider.TryGetFolderFromPathAsync(dir);
        var files = await top.StorageProvider.OpenFilePickerAsync(options);
        return files.FirstOrDefault()?.TryGetLocalPath();
    }

    static TopLevel? FindWindow()
    {
        if (Application.Current?.ApplicationLifetime is not IClassicDesktopStyleApplicationLifetime desktop)
            return null;
        if (desktop.MainWindow is { IsVisible: true } main) return main;
        return desktop.Windows.FirstOrDefault(w => w.IsActive)
            ?? desktop.Windows.FirstOrDefault(w => w.IsVisible);
    }

    void OnCommand(string[] args, Action<string> writeLine)
    {
        var verb = args.Length > 0 ? args[0].ToLowerInvariant() : "status";
        var rest = args.Skip(1).ToArray();
        switch (verb)
        {
            case "load":
                // Radegast's command parser treats \ as an escape and drops it, so a
                // typed Windows path arrives mangled. No path opens the file picker;
                // a typed path should use forward slashes.
                if (rest.Length == 0) { OnLoadMenu(); return; }
                Load(string.Join(" ", rest).Trim('"'), writeLine);
                return;
            case "fetch": Start("fetch", Fetch, writeLine); return;
            case "check": Check(writeLine); return;
            case "apply": Apply(rest, writeLine); return;
            case "verify": Start("verify", Verify, writeLine); return;
            case "undo": Undo(rest, writeLine); return;
            case "stop":
                _cts?.Cancel();
                writeLine("stopping after the current step");
                return;
            case "export": Start("export", Export, writeLine); return;
            case "remote": Remote(rest, writeLine); return;
            case "status": Status(writeLine); return;
            default: writeLine(Usage); return;
        }
    }

    bool Busy(Action<string> writeLine)
    {
        if (_running is { IsCompleted: false })
        {
            writeLine("busy: wait for the current step, or '//hangar stop'");
            return true;
        }
        return false;
    }

    void Start(string name, Func<CancellationToken, Task> work, Action<string> writeLine)
    {
        if (Busy(writeLine)) return;
        if (!_ctx.Client.Network.Connected) { writeLine("not logged in"); return; }
        _cts?.Dispose();
        _cts = new CancellationTokenSource();
        var ct = _cts.Token;
        _startCount++;
        _running = Task.Run(async () =>
        {
            try { await work(ct).ConfigureAwait(false); }
            catch (OperationCanceledException) { Say($"{name}: stopped"); }
            catch (Exception ex) { Say($"{name} failed: {ex.Message}"); }
        });
        writeLine($"{name}: started");
    }

    void Load(string path, Action<string> writeLine)
    {
        if (Busy(writeLine)) return;
        try
        {
            using var r = new StreamReader(path);
            _plan = PlanFile.Read(r);
            _planPath = path;
            _ctx.SetSetting("last_plan", Path.GetFullPath(path));
            _checked = null;
            writeLine($"loaded {_plan.Describe()} from {path}. Next: //hangar fetch");
        }
        catch (FileNotFoundException) when (path.Length > 2 && path[1] == ':' && path.IndexOfAny(new[] { '/', '\\' }) < 0)
        {
            writeLine($"could not find '{path}': the chat box drops backslashes. Use forward slashes " +
                      "(//hangar load W:/folder/plan.csv) or just //hangar load to pick the file");
        }
        catch (Exception ex) { writeLine("could not read the plan: " + ex.Message); }
    }

    async Task Fetch(CancellationToken ct)
    {
        // A fetch that is running, or fails, leaves nothing trusted and no check standing.
        _fetched = false;
        _confirmed = null;
        _items = null;
        _checked = null;
        Say("fetching the whole inventory from the server; this takes a few minutes");
        var clock = System.Diagnostics.Stopwatch.StartNew();
        var r = await new Operations(_ctx.Client, Say).FetchAllAsync(ct).ConfigureAwait(false);
        _confirmed = r.Confirmed;
        _items = new ItemIndex(r.Items);
        _unanswered = r.Unanswered.Count;
        _fetched = true;
        _checked = null;
        Say($"fetched {r.Fetched} folders in {Elapsed.Format(clock.Elapsed)}; the server confirmed {r.Confirmed.Count} folders and listed {r.Items.Count} items");
        if (r.Removed.Count > 0)
            Say($"removed {r.Removed.Count} phantom folders the server does not have from the local store: " +
                string.Join(", ", r.Removed.Take(10)) + (r.Removed.Count > 10 ? ", ..." : ""));
        if (r.KeptSystem.Count > 0)
            Say($"{r.KeptSystem.Count} local system folders are not on the server (left alone): " +
                string.Join(", ", r.KeptSystem.Take(10)));
        if (r.KeptWithItems.Count > 0)
            Say($"{r.KeptWithItems.Count} folders the server does not have were left in the store because " +
                "they hold items or system folders: " + string.Join(", ", r.KeptWithItems.Take(10)));
        if (r.Unanswered.Count > 0)
            Say($"WARNING: the server did not answer for {r.Unanswered.Count} folders (first ten: " +
                string.Join(", ", r.Unanswered.Take(10)) + "). Their contents count as unknown; fetch again if they matter.");
        Say("Next: //hangar check");
    }

    async Task Export(CancellationToken ct)
    {
        if (!_fetched || _confirmed == null)
        {
            Say("fetch the live inventory first: //hangar fetch (export only writes folders the server confirmed in this session)");
            return;
        }
        var store = _ctx.Client.Inventory.Store
            ?? throw new InvalidOperationException("the inventory is not loaded yet; wait for login to finish");
        var source = new StoreExportSource(store);
        var content = InventoryExporter.Build(source, _confirmed);
        Say($"exporting {content.Folders.Count} folders and {content.Items.Count} items; looking up {content.CreatorIds.Count} creator names");
        var creators = await new NameLookup(_ctx.Client, Say)
            .ResolveAsync(content.CreatorIds, TimeSpan.FromSeconds(60), ct).ConfigureAwait(false);
        int unresolved = content.CreatorIds.Count(c => !creators.ContainsKey(c));

        var self = _ctx.Client.Self;
        var last = _ctx.GetSetting("last_plan");
        var dir = last != null && Path.GetDirectoryName(last) is { Length: > 0 } d && Directory.Exists(d)
            ? d : Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments);
        var path = Path.Combine(dir, InventoryExporter.FileName(self.Name, DateTime.Now));
        var tmp = path + ".tmp";
        using (var fs = File.Create(tmp))
            InventoryExporter.Write(fs, DateTimeOffset.Now, self.AgentID.ToString(), self.Name, source.RootId, content, creators);
        File.Move(tmp, path, true);
        if (_unanswered > 0)
            Say($"WARNING: the server did not answer for {_unanswered} folders in the fetch; their items are missing from the export. Fetch again if they matter.");
        Say($"exported {content.Folders.Count} folders, {content.Items.Count} items, {creators.Count} creators ({unresolved} unresolved) to {path}");
    }

    void Check(Action<string> writeLine)
    {
        if (Busy(writeLine)) return;
        if (_plan == null) { writeLine("load a plan first: //hangar load <plan.csv>"); return; }
        if (!_fetched || _confirmed == null || _items == null) { writeLine("fetch the live inventory first: //hangar fetch (check only trusts folders and items the server listed in this session)"); return; }
        _checked = _plan.Format == PlanFormat.Actions
            ? ActionChecker.Check(_plan.ActionRows, Tree(), _items)
            : PlanChecker.Check(_plan.FolderRows, Tree());
        var preview = PreviewPath();
        using (var w = new StreamWriter(preview)) _checked.WritePreview(w);
        writeLine(_checked.Summary());
        foreach (var g in _checked.SkipReasons.GroupBy(reason => reason.Split('(')[0].Trim()).OrderByDescending(g => g.Count()).Take(6))
            writeLine($"  skipped {g.Count()}: {g.Key}");
        writeLine($"preview written to {preview}. Read it, then: //hangar apply  (or '//hangar apply 5' to try five first)");
    }

    void Apply(string[] rest, Action<string> writeLine)
    {
        if (_checked == null) { writeLine("check the plan first: //hangar check"); return; }
        int limit = int.MaxValue;
        if (rest.Length > 0 && (!int.TryParse(rest[0], out limit) || limit <= 0))
        {
            writeLine("usage: //hangar apply [N]  (N = only the first N moves)");
            return;
        }
        var plan = _checked;
        _checked = null;     // a plan is applied once; check again to continue
        if (plan is CheckedActionPlan actions)
            Start("apply", ct => ApplyActionsAsync(actions, limit, ct), writeLine);
        else
            Start("apply", ct => ApplyAsync((CheckedPlan)plan, limit, ct), writeLine);
    }

    async Task ApplyAsync(CheckedPlan plan, int limit, CancellationToken ct)
    {
        var ops = new Operations(_ctx.Client, Say);
        var tree = Tree();
        var moves = plan.Moves.Take(limit).ToList();
        var creates = FoldersNeeded(plan.FoldersToCreate, moves.Where(m => m.DestinationId == null).Select(m => m.Row.To));

        // Existing destinations (and existing parents of folders to create) must still be on the
        // server, not just in the local store, before anything is created or moved.
        var existing = moves.Where(m => m.DestinationId != null).Select(m => m.DestinationId!);
        if (!await DestinationsOnServerAsync(ops, tree, existing, creates, $"{moves.Count} moves", ct).ConfigureAwait(false)) return;

        using var journal = OpenJournal();
        var made = await CreateDestinationsAsync(ops, tree, creates, journal, $"all {moves.Count} moves", ct).ConfigureAwait(false);
        if (made == null) return;

        var list = new List<(UUID, UUID, UUID, string)>();
        var renames = new List<(UUID, UUID, string, string)>();
        foreach (var m in moves)
        {
            UUID to;
            if (m.DestinationId != null) to = UUID.Parse(m.DestinationId);
            else if (made.TryGetValue(string.Join(PlanReader.PathSeparator, PlanReader.SplitPath(m.Row.To)), out var id)) to = id;
            else if (Operations.ResolvePath(tree, m.Row.To) is { } found) to = found;
            else { Say($"skipped {m.Row.ProductFolder}: destination was not created"); continue; }
            var folder = UUID.Parse(m.Row.FolderId);
            var from = UUID.Parse(m.CurrentParentId!);
            if (from != to) list.Add((folder, from, to, m.Row.ProductFolder));
            if (m.Rename != null) renames.Add((folder, to, m.Row.ProductFolder, m.Rename));
        }
        var (moved, failed) = await ops.MoveAsync(list, journal, ct).ConfigureAwait(false);
        _lastMoves = moved;
        _lastItemMoves = null;
        // A folder that did not move is not renamed either: it would be renamed in the wrong place.
        var failedIds = failed.Select(f => f.Folder).ToHashSet();
        var skippedRenames = renames.Where(r => failedIds.Contains(r.Item1)).ToList();
        var (renamed, renameFailed) = await ops.RenameAsync(
            renames.Where(r => !failedIds.Contains(r.Item1)).ToList(), journal, ct).ConfigureAwait(false);
        _lastRenames = renamed;

        await ops.ReconcileAsync(list, renames, ct).ConfigureAwait(false);

        var moveLine = ApplyReport.FailureLine("move", failed.Select(f => f.Name).ToList());
        if (moveLine.Length > 0) Say(moveLine + ". They stay where they were; check and apply again to retry.");
        var renameLine = ApplyReport.FailureLine("rename",
            renameFailed.Concat(skippedRenames).Select(r => r.Item3 + " -> " + r.Item4).ToList());
        if (renameLine.Length > 0) Say(renameLine);
        Say(ApplyReport.Summary(moved.Count, failed.Count + skippedRenames.Count + renameFailed.Count,
            renamed.Count, ops.CreatedCount) + ". Verifying with the server…");
        await Task.Delay(TimeSpan.FromSeconds(5), ct).ConfigureAwait(false);
        await Verify(ct).ConfigureAwait(false);
    }

    /// <summary>The folders to create for these destination paths: each path and every parent above it,
    /// taken from the checked plan's list so that parents stay before children.</summary>
    static List<string> FoldersNeeded(IEnumerable<string> foldersToCreate, IEnumerable<string> destinationPaths)
    {
        var needed = destinationPaths
            .SelectMany(to =>
            {
                var parts = PlanReader.SplitPath(to);
                return Enumerable.Range(1, parts.Length).Select(i => string.Join(PlanReader.PathSeparator, parts.Take(i)));
            })
            .ToHashSet();
        return foldersToCreate.Where(needed.Contains).ToList();
    }

    /// <summary>Existing destinations (and existing parents of folders to create) must still be on the
    /// server, not just in the local store, before anything is created or moved. False (after saying
    /// so) if one is not.</summary>
    async Task<bool> DestinationsOnServerAsync(Operations ops, IFolderTree tree, IEnumerable<string> destinationIds,
        List<string> creates, string undone, CancellationToken ct)
    {
        var existing = destinationIds.Select(UUID.Parse).ToList();
        var createSet = creates.ToHashSet();
        foreach (var path in creates)
        {
            var parts = PlanReader.SplitPath(path);
            var parentPath = string.Join(PlanReader.PathSeparator, parts.Take(parts.Length - 1));
            if (parentPath.Length > 0 && !createSet.Contains(parentPath)
                && Operations.ResolvePath(tree, parentPath) is { } pid) existing.Add(pid);
        }
        var unconfirmed = await ops.UnconfirmedAsync(existing, ct).ConfigureAwait(false);
        if (unconfirmed.Count == 0) return true;
        Say($"STOPPED: the server does not list {unconfirmed.Count} destination folders: " +
            string.Join(", ", unconfirmed.Take(10)) + (unconfirmed.Count > 10 ? ", ..." : "") +
            $". Nothing was created or moved ({undone} left undone). Fetch, check, apply again.");
        return false;
    }

    /// <summary>Starts the journal for an apply, before anything is sent.</summary>
    Journal OpenJournal()
    {
        var journalPath = JournalPath();
        var journal = new Journal(journalPath);
        _lastJournal = journalPath;
        _ctx.SetSetting("last_journal", journalPath);
        Say($"journal: {journalPath}");
        return journal;
    }

    /// <summary>Creates the missing destination folders, each confirmed by the server. Null (after
    /// saying so) if the server never confirms one: nothing may then move into it.</summary>
    async Task<Dictionary<string, UUID>?> CreateDestinationsAsync(Operations ops, IFolderTree tree,
        List<string> creates, Journal journal, string left, CancellationToken ct)
    {
        Dictionary<string, UUID> made;
        try { made = await ops.CreateFoldersAsync(creates, tree, journal, ct).ConfigureAwait(false); }
        catch (FolderNotConfirmedException ex)
        {
            Say($"STOPPED: {ex.Message}. Nothing was moved into a folder the server has not confirmed, " +
                $"and {left} were left undone. Folders already created stay (see the journal). " +
                "Try again later: fetch, check, apply.");
            Say(ApplyReport.Summary(0, 0, 0, ops.CreatedCount));
            return null;
        }
        _confirmed?.UnionWith(made.Values.Select(v => v.ToString()));
        if (ops.CreatedCount > 0) Say($"created {ops.CreatedCount} folders (confirmed by the server)");
        return made;
    }

    async Task ApplyActionsAsync(CheckedActionPlan plan, int limit, CancellationToken ct)
    {
        var ops = new Operations(_ctx.Client, Say);
        var tree = Tree();
        var todo = plan.Todo.Take(limit).ToList();
        var creates = FoldersNeeded(plan.FoldersToCreate, todo.Where(a => a.DestinationId == null).Select(a => a.Row.To));
        var existing = todo.Where(a => a.DestinationId != null).Select(a => a.DestinationId!).Distinct();
        if (!await DestinationsOnServerAsync(ops, tree, existing, creates, $"{todo.Count} actions", ct).ConfigureAwait(false)) return;

        using var journal = OpenJournal();
        var made = await CreateDestinationsAsync(ops, tree, creates, journal, $"all {todo.Count} actions", ct).ConfigureAwait(false);
        if (made == null) return;

        var itemList = new List<(UUID Item, UUID From, UUID To, string Name)>();
        var folderList = new List<(UUID Folder, UUID From, UUID To, string Name)>();
        foreach (var a in todo)
        {
            UUID to;
            if (a.DestinationId != null) to = UUID.Parse(a.DestinationId);
            else if (made.TryGetValue(string.Join(PlanReader.PathSeparator, PlanReader.SplitPath(a.Row.To)), out var madeId)) to = madeId;
            else if (Operations.ResolvePath(tree, a.Row.To) is { } found) to = found;
            else { Say($"skipped {a.Row.ItemName}: destination was not created"); continue; }
            var id = UUID.Parse(a.Row.ItemId);
            var from = UUID.Parse(a.CurrentParentId!);
            if (from == to) continue;
            if (a.Row.Kind == ActionKind.FolderTrash) folderList.Add((id, from, to, a.Row.ItemName));
            else itemList.Add((id, from, to, a.Row.ItemName));
        }

        // Items first, then folders: an item taken out of a folder that is itself going to Trash
        // is moved while that folder is still where the plan found it.
        var (itemsMoved, itemsFailed) = await ops.MoveItemsAsync(itemList, journal, ct).ConfigureAwait(false);
        foreach (var m in itemsMoved) _items?.SetParent(m.Item.ToString(), m.To.ToString());
        var (foldersMoved, foldersFailed) = await ops.MoveAsync(folderList, journal, ct).ConfigureAwait(false);
        _lastItemMoves = itemsMoved;
        _lastMoves = foldersMoved;
        _lastRenames = null;
        await ops.ReconcileAsync(folderList, new List<(UUID, UUID, string, string)>(), ct).ConfigureAwait(false);

        var trashId = tree.TrashFolder()?.Id;
        int itemsTrashed = itemsMoved.Count(m => m.To.ToString() == trashId);
        var failedNames = itemsFailed.Select(f => f.Name).Concat(foldersFailed.Select(f => f.Name)).ToList();
        var failLine = ApplyReport.FailureLine("move", failedNames);
        if (failLine.Length > 0) Say(failLine + ". They stay where they were; check and apply again to retry.");
        Say(ApplyReport.ActionSummary(itemsMoved.Count - itemsTrashed, itemsTrashed, foldersMoved.Count,
            failedNames.Count, ops.CreatedCount) + ". Verifying with the server…");
        await Task.Delay(TimeSpan.FromSeconds(5), ct).ConfigureAwait(false);
        await Verify(ct).ConfigureAwait(false);
    }

    async Task Verify(CancellationToken ct)
    {
        var ops = new Operations(_ctx.Client, Say);
        bool any = false;
        if (_lastRenames is { Count: > 0 })
        {
            any = true;
            var wrong = await ops.VerifyRenamesAsync(_lastRenames, ct).ConfigureAwait(false);
            if (wrong.Count == 0) Say($"verified: all {_lastRenames.Count} renames took");
            else foreach (var w in wrong) Say("  not renamed yet: " + w);
        }
        if (_lastMoves is { Count: > 0 })
        {
            any = true;
            var missing = await ops.VerifyAsync(_lastMoves, ct).ConfigureAwait(false);
            if (missing.Count == 0) Say($"verified: all {_lastMoves.Count} folders are where they should be");
            else
            {
                Say($"{missing.Count} of {_lastMoves.Count} folders are not in their destination yet:");
                foreach (var m in missing.Take(10)) Say("  " + m.Name);
                Say("the server may still be catching up: try '//hangar verify' in a minute");
            }
        }
        if (_lastItemMoves is { Count: > 0 })
        {
            any = true;
            var missing = await ops.VerifyItemsAsync(_lastItemMoves, ct).ConfigureAwait(false);
            if (missing.Count == 0) Say($"verified: all {_lastItemMoves.Count} items are where they should be");
            else
            {
                Say($"{missing.Count} of {_lastItemMoves.Count} items are not in their destination yet:");
                foreach (var m in missing.Take(10)) Say("  " + m.Name);
                Say("the server may still be catching up: try '//hangar verify' in a minute");
            }
        }
        if (!any) Say("nothing applied in this session to verify");
    }

    void Undo(string[] rest, Action<string> writeLine)
    {
        var path = rest.Length > 0 ? string.Join(" ", rest).Trim('"') : _lastJournal;
        if (path == null || !File.Exists(path)) { writeLine("no journal to undo: //hangar undo <journal.tsv>"); return; }
        if (!_fetched || _confirmed == null) { writeLine("fetch the live inventory first, so undo checks where things are now: //hangar fetch"); return; }
        List<JournalEntry> entries;
        using (var r = new StreamReader(path)) entries = Journal.Read(r);
        var (moves, renamesBack, folderSkipped, created) = Journal.PlanUndoAll(entries, Tree());
        var (itemMovesBack, itemSkipped) = Journal.PlanUndoItems(entries, _items ?? new ItemIndex(), Tree());
        var skipped = folderSkipped.Concat(itemSkipped).ToList();
        writeLine($"undo: {moves.Count} folders go back, {itemMovesBack.Count} items go back, {renamesBack.Count} get their old names back, {skipped.Count} skipped");
        foreach (var s in skipped.Take(10)) writeLine("  skipped " + s);
        if (created.Count > 0)
            writeLine($"  {created.Count} folders Hangar created are left in place (empty ones can be deleted by hand)");
        if (moves.Count == 0 && renamesBack.Count == 0 && itemMovesBack.Count == 0) return;
        var list = moves.Select(m => (UUID.Parse(m.FolderId), UUID.Parse(m.OldParentId), UUID.Parse(m.NewParentId), m.Name)).ToList();
        var itemList = itemMovesBack.Select(m => (UUID.Parse(m.FolderId), UUID.Parse(m.OldParentId), UUID.Parse(m.NewParentId), m.Name)).ToList();
        // Rename back first, in place, then move back; the move does not touch names.
        var renames = renamesBack.Select(r => (UUID.Parse(r.FolderId), UUID.Parse(r.NewParentId), r.OldName, r.Name)).ToList();
        Start("undo", async ct =>
        {
            using var journal = new Journal(JournalPath("undo"));
            var ops = new Operations(_ctx.Client, Say);
            var (renamedBack, renameFailed) = await ops.RenameAsync(renames, journal, ct).ConfigureAwait(false);
            _lastRenames = null;
            var (movedBack, failed) = await ops.MoveAsync(list, journal, ct).ConfigureAwait(false);
            _lastMoves = movedBack;
            // Folders first: an item may go back into a folder that has just come out of Trash.
            var (itemsBack, itemsFailed) = await ops.MoveItemsAsync(itemList, journal, ct).ConfigureAwait(false);
            _lastItemMoves = itemsBack;
            foreach (var m in itemsBack) _items?.SetParent(m.Item.ToString(), m.To.ToString());
            await ops.ReconcileAsync(list, renames, ct).ConfigureAwait(false);
            var moveLine = ApplyReport.FailureLine("move back", failed.Select(f => f.Name).Concat(itemsFailed.Select(f => f.Name)).ToList());
            if (moveLine.Length > 0) Say(moveLine);
            var renameLine = ApplyReport.FailureLine("rename back", renameFailed.Select(r => r.NewName + " -> " + r.OldName).ToList());
            if (renameLine.Length > 0) Say(renameLine);
            Say($"undo finished: moved back {movedBack.Count} folders and {itemsBack.Count} items, " +
                $"failed {failed.Count + itemsFailed.Count}, renamed back {renamedBack.Count}");
            await Task.Delay(TimeSpan.FromSeconds(5), ct).ConfigureAwait(false);
            await Verify(ct).ConfigureAwait(false);
        }, writeLine);
    }

    void Status(Action<string> writeLine)
    {
        writeLine(_plan == null ? "no plan loaded" : $"plan: {_planPath} ({_plan.Describe()})");
        writeLine(_fetched ? "inventory fetched live this session" : "inventory not fetched yet");
        writeLine(_checked == null ? "not checked (or already applied)" : "checked: " + _checked.Summary());
        if (_running is { IsCompleted: false }) writeLine("a step is running");
        if (_lastJournal != null) writeLine("last journal: " + _lastJournal);
        if (_remote is { IsOn: true }) writeLine($"remote control is on (http://127.0.0.1:{_remote.Port}/)");
    }

    void Remote(string[] rest, Action<string> writeLine)
    {
        switch (rest.Length > 0 ? rest[0].ToLowerInvariant() : "status")
        {
            case "on":
                if (_fromRemote) { writeLine("remote on can only be typed in Veles chat"); return; }
                if (_remote is { IsOn: true }) { writeLine($"remote control is already on (port {_remote.Port})"); return; }
                if (!_ctx.Client.Network.Connected) { writeLine("not logged in"); return; }
                var port = RemotePolicy.ParsePort(Environment.GetEnvironmentVariable("HANGAR_REMOTE_PORT"));
                try
                {
                    _remote?.Dispose();
                    _remote = new RemoteControl(_log, this, _ctx.Client.Self.Name);
                    _remote.Start(port);
                    _chatSwitch = true;
                    writeLine($"remote control on: http://127.0.0.1:{_remote.Port}/ (this machine only, token in {_remote.TokenFile}). " +
                              "A program on this machine can now run Hangar commands, apply and undo included. '//hangar remote off' ends it.");
                }
                catch (Exception ex)
                {
                    _remote?.Dispose();
                    _remote = null;
                    writeLine($"could not start remote control on ports {port}-{port + RemotePolicy.PortTries - 1}: {ex.Message}");
                }
                return;
            case "off":
                if (_remote is not { IsOn: true }) { writeLine("remote control is off"); return; }
                writeLine("remote control off");
                if (_fromRemote) _stopRemoteAfter = true; else StopRemote();
                return;
            case "status":
                writeLine(_remote is { IsOn: true }
                    ? $"remote control is on: http://127.0.0.1:{_remote.Port}/"
                    : "remote control is off ('//hangar remote on' to start it)");
                return;
            default:
                writeLine("usage: //hangar remote on|off|status");
                return;
        }
    }

    void StopRemote()
    {
        _chatSwitch = false;
        _remote?.Dispose();
        _remote = null;
    }

    RemoteState IRemoteHost.State => new(_running is { IsCompleted: false }, _chatSwitch);

    object IRemoteHost.Status()
    {
        var self = _ctx.Client.Self;
        return new
        {
            avatar = _ctx.Client.Network.Connected ? self.Name : null,
            loaded = _plan == null ? null : _planPath,
            fetched = _fetched,
            busy = _running is { IsCompleted: false },
        };
    }

    RemoteRun IRemoteHost.Run(string text, string[] args)
    {
        Say("remote: " + text);
        long start = _log.LastSeq;
        int before = _startCount;
        _fromRemote = true;
        _stopRemoteAfter = false;
        try { OnCommand(args, Say); }
        catch (Exception ex) { Say("failed: " + ex.Message); }
        finally { _fromRemote = false; }
        var (lines, next, _) = _log.Since(start);
        return new RemoteRun(_startCount != before, lines.ConvertAll(l => l.Text), next, _stopRemoteAfter);
    }

    IFolderTree Tree() => new ConfirmedTree(
        new StoreTree(_ctx.Client.Inventory.Store
            ?? throw new InvalidOperationException("the inventory is not loaded yet; wait for login to finish")),
        _confirmed ?? throw new InvalidOperationException("fetch the live inventory first: //hangar fetch"));

    string Dir() => Path.GetDirectoryName(Path.GetFullPath(_planPath ?? "plan.csv")) ?? ".";

    string PreviewPath() => Path.Combine(Dir(), Path.GetFileNameWithoutExtension(_planPath ?? "plan") + ".preview.csv");

    string JournalPath(string kind = "apply") =>
        Path.Combine(Dir(), $"hangar-{kind}-{DateTime.Now:yyyyMMdd-HHmmss}.tsv");
}
