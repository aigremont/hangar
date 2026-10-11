using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Net.Http;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using LibreMetaverse;
using LibreMetaverse.Packets;
using LibreMetaverse.StructuredData;
using Veles.Plugin.Hangar.Core;

namespace Veles.Plugin.Hangar.Live;

/// <summary>A new folder the server never confirmed; the apply stops rather than move into it.</summary>
public sealed class FolderNotConfirmedException : Exception
{
    public string FolderName { get; }
    public FolderNotConfirmedException(string name)
        : base($"the server did not confirm the new folder '{name}'") => FolderName = name;
}

/// <summary>Talks to the grid: fetch, create, move (viewer UDP message, confirmed by listing the destination), verify. Pacing is deliberately gentle.</summary>
public sealed class Operations
{
    // At most this many folder fetches in flight at once.
    const int FetchConcurrency = 4;
    // Folder moves per MoveInventoryFolder message, and the pause between messages.
    const int MoveBatch = 20;
    static readonly TimeSpan MovePause = TimeSpan.FromSeconds(2);
    // Item moves per MoveInventoryItem message (each block is 33 bytes, so 30 fit one packet).
    // Same pause as for folders; progress is reported every ItemProgress.
    const int ItemMoveBatch = 30;
    static readonly TimeSpan ItemProgress = TimeSpan.FromSeconds(30);
    static readonly TimeSpan CreatePause = TimeSpan.FromMilliseconds(500);
    // After the move messages are sent, the destinations are listed by the server this often,
    // for up to this long, before a move is called missing; the missing ones are resent once.
    static readonly TimeSpan MovePoll = TimeSpan.FromSeconds(2);
    static readonly TimeSpan MoveConfirmTimeout = TimeSpan.FromSeconds(20);
    // The pause before a refused rename or create is retried.
    static readonly TimeSpan RetryPause = TimeSpan.FromSeconds(3);
    // A created folder must show up on the server within this long, polled this often.
    static readonly TimeSpan ConfirmTimeout = TimeSpan.FromSeconds(30);
    static readonly TimeSpan ConfirmPoll = TimeSpan.FromSeconds(1);

    readonly GridClient _client;
    readonly Action<string> _say;

    public Operations(GridClient client, Action<string> say)
    {
        _client = client;
        _say = say;
    }

    Inventory Store => _client.Inventory.Store
        ?? throw new InvalidOperationException("the inventory is not loaded yet; wait for login to finish");

    UUID Owner => _client.Self.AgentID;

    /// <summary>What a full fetch learned from the server.</summary>
    public sealed record FetchResult(
        int Fetched, HashSet<string> Confirmed, List<string> Unanswered,
        List<string> Removed, List<string> KeptSystem, List<string> KeptWithItems, List<ItemInfo> Items);

    // LibreMetaverse swallows a failed folder fetch and returns an empty list, which looks the
    // same as an empty folder. It does set DescendentCount from the reply, so a sentinel put
    // there beforehand tells an answered request from an unanswered one.
    const int Unanswered = -1;

    // Fast fetch: up to this many folders per FetchInventoryDescendents2 request, and at most
    // this many requests in flight. A folder the batch does not answer cleanly is listed again
    // on its own by the per-folder call, so the result is the same as folder-by-folder.
    const int FetchWorkers = 8;
    const int FetchBatch = 20;
    const string FetchCap = "FetchInventoryDescendents2";
    static readonly TimeSpan FetchRetryPause = TimeSpan.FromSeconds(2);

    /// <summary>How one folder was answered: by a batched reply (Fast), by the per-folder call
    /// (Listed), or not at all (both null).</summary>
    sealed record FolderAnswer(OSDMap? Fast, List<InventoryBase>? Listed);

    sealed class FetchStats
    {
        public int Requests, Relisted, Failures;
        public volatile bool FastOff;
    }

    /// <summary>Fetches every folder of the user's inventory from the server once, so the
    /// checks run against live data rather than a cache. Records which folders the server
    /// confirmed, puts moved folders under the parent the server reports, and removes from the
    /// local store the ordinary folders the server does not have (leftovers of folder creates
    /// that never reached it). System folders and folders holding items are never removed.</summary>
    public async Task<FetchResult> FetchAllAsync(CancellationToken ct)
    {
        var root = Store.RootFolder ?? throw new InvalidOperationException("no inventory root yet");
        var fetched = new HashSet<UUID>();
        var confirmed = new HashSet<string> { root.UUID.ToString() };
        var serverParent = new Dictionary<UUID, UUID>();
        var localKids = new List<InventoryFolder>();
        var unanswered = new List<string>();
        var items = new Dictionary<string, ItemInfo>();
        var queue = new List<UUID> { root.UUID };
        var seen = new HashSet<UUID> { root.UUID };   // queued, in flight or done
        var stats = new FetchStats();
        var inFlight = new Dictionary<Task<Dictionary<UUID, FolderAnswer>>, List<UUID>>();
        var clock = Stopwatch.StartNew();
        var lastReport = TimeSpan.Zero;
        // Only this loop touches the shared collections and the store; the tasks do network only.
        while (queue.Count > 0 || inFlight.Count > 0)
        {
            ct.ThrowIfCancellationRequested();
            while (inFlight.Count < FetchWorkers && queue.Count > 0)
            {
                int n = FetchBatches.NextSize(queue.Count, FetchWorkers, FetchBatch);
                var ids = queue.GetRange(0, n);
                queue.RemoveRange(0, n);
                inFlight[FetchChunkAsync(ids, stats, ct)] = ids;
            }
            var done = await Task.WhenAny(inFlight.Keys).ConfigureAwait(false);
            var batch = inFlight[done];
            inFlight.Remove(done);
            var answers = await done.ConfigureAwait(false);

            foreach (var id in batch)
            {
                fetched.Add(id);
                List<InventoryBase>? contents = null;
                if (answers.TryGetValue(id, out var a))
                    contents = a.Fast != null ? Ingest(a.Fast) : a.Listed;
                if (contents == null)
                {
                    unanswered.Add(Store.TryGetValue(id, out var nb) ? nb.Name : id.ToString());
                    continue;
                }
                try { localKids.AddRange(Store.GetContents(id).OfType<InventoryFolder>()); }
                catch { /* no local children to compare */ }
                // Items are recorded under the folder that was listed, as the server answered.
                foreach (var it in contents.OfType<InventoryItem>())
                    items[it.UUID.ToString()] = new ItemInfo(it.UUID.ToString(), it.Name, id.ToString(), it.AssetType.ToString());
                foreach (var f in contents.OfType<InventoryFolder>())
                {
                    confirmed.Add(f.UUID.ToString());
                    serverParent[f.UUID] = id;
                    if (seen.Add(f.UUID)) queue.Add(f.UUID);
                }
            }
            if (clock.Elapsed - lastReport >= TimeSpan.FromSeconds(30))
            {
                lastReport = clock.Elapsed;
                _say($"fetching: {fetched.Count} folders done, {queue.Count} queued");
            }
        }
        if (stats.Requests > 0 || stats.Failures > 0)
            _say($"fetch: {stats.Requests} batched requests, {stats.Relisted} folders listed one by one" +
                 (stats.FastOff ? " (batched fetch switched off after repeated failures)" : ""));

        // No request is in flight now, so the store can be changed without racing a fetch.
        foreach (var (folder, parent) in serverParent) StoreSetParent(folder, parent);
        var (removed, keptSystem, keptItems) = PrunePhantoms(localKids, confirmed);
        return new FetchResult(fetched.Count, confirmed, unanswered, removed, keptSystem, keptItems, items.Values.ToList());
    }

    /// <summary>Lists these folders: one batched request first, then the per-folder call for any
    /// folder the batch did not answer or answered with a listing that does not add up.
    /// Never throws except on cancellation.</summary>
    async Task<Dictionary<UUID, FolderAnswer>> FetchChunkAsync(List<UUID> ids, FetchStats stats, CancellationToken ct)
    {
        var answers = new Dictionary<UUID, FolderAnswer>();
        if (!stats.FastOff)
        {
            var fast = await TryBatchAsync(ids, stats, ct).ConfigureAwait(false);
            if (fast != null)
                foreach (var (id, m) in fast)
                    if (CountsAgree(m)) answers[id] = new FolderAnswer(m, null);
        }
        foreach (var id in ids)
        {
            if (answers.ContainsKey(id)) continue;
            Interlocked.Increment(ref stats.Relisted);
            answers[id] = new FolderAnswer(null, await ListOneAsync(id, ct).ConfigureAwait(false));
        }
        return answers;
    }

    static bool CountsAgree(OSDMap m) =>
        FetchBatches.CountsAgree(m["descendents"].AsInteger(),
            (m["categories"] as OSDArray)?.Count ?? 0, (m["items"] as OSDArray)?.Count ?? 0);

    /// <summary>One FetchInventoryDescendents2 POST for several folders. Null when the
    /// capability is missing or the request failed twice (three such chunks switch the batched
    /// fetch off); otherwise the folder entries the server returned, by folder id.</summary>
    async Task<Dictionary<UUID, OSDMap>?> TryBatchAsync(List<UUID> ids, FetchStats stats, CancellationToken ct)
    {
        var uri = _client.Network.CurrentSim?.Caps?.CapabilityURI(FetchCap);
        if (uri == null) { stats.FastOff = true; return null; }
        var wanted = ids.ToHashSet();
        var request = new OSDArray(ids.Count);
        foreach (var id in ids)
            request.Add(new OSDMap(5)
            {
                ["folder_id"] = OSD.FromUUID(id),
                ["owner_id"] = OSD.FromUUID(Owner),
                ["fetch_folders"] = OSD.FromBoolean(true),
                ["fetch_items"] = OSD.FromBoolean(true),
                ["sort_order"] = OSD.FromInteger((int)InventorySortOrder.ByName),
            });
        var payload = new OSDMap(1) { ["folders"] = request };
        for (int attempt = 0; attempt < 2; attempt++)
        {
            ct.ThrowIfCancellationRequested();
            try
            {
                var (resp, data) = await _client.HttpCapsClient.PostAsync(uri, OSDFormat.Xml, payload, ct).ConfigureAwait(false);
                if (resp.IsSuccessStatusCode && data is { Length: > 0 }
                    && OSDParser.Deserialize(data) is OSDMap reply && reply["folders"] is OSDArray folders)
                {
                    var byId = new Dictionary<UUID, OSDMap>();
                    foreach (var f in folders.OfType<OSDMap>())
                    {
                        var fid = f["folder_id"].AsUUID();
                        if (wanted.Contains(fid)) byId[fid] = f;
                    }
                    if (FetchBatches.Plausible(ids.Count, byId.Count))
                    {
                        Interlocked.Increment(ref stats.Requests);
                        return byId;
                    }
                }
            }
            catch (OperationCanceledException) { throw; }
            catch { /* retried once, then listed folder by folder */ }
            if (attempt == 0) await Task.Delay(FetchRetryPause, ct).ConfigureAwait(false);
        }
        if (Interlocked.Increment(ref stats.Failures) >= 3) stats.FastOff = true;
        return null;
    }

    /// <summary>Turns a batched reply entry into the folders and items it lists, and records them
    /// in the local store the way LibreMetaverse's own per-folder call does. A moved folder keeps
    /// its old local parent here; the fetch sets parents from the listings afterwards.</summary>
    List<InventoryBase> Ingest(OSDMap entry)
    {
        var ret = new List<InventoryBase>();
        var id = entry["folder_id"].AsUUID();
        if (!(Store.TryGetValue(id, out var b) && b is InventoryFolder folder))
        {
            folder = new InventoryFolder(id);
            Store[id] = folder;
        }
        folder.DescendentCount = entry["descendents"].AsInteger();
        folder.Version = entry["version"].AsInteger();
        folder.OwnerID = entry["owner_id"].AsUUID();
        if (Store.TryGetNodeFor(id, out var node)) node.NeedsUpdate = false;
        if (folder.DescendentCount <= 0) return ret;

        if (entry["categories"] is OSDArray cats)
            foreach (var c in cats.OfType<OSDMap>())
            {
                var cid = (c.TryGetValue("category_id", out var cv) ? cv : c["folder_id"]).AsUUID();
                if (!(Store.TryGetValue(cid, out var cb) && cb is InventoryFolder cf))
                {
                    cf = new InventoryFolder(cid) { ParentUUID = c["parent_id"].AsUUID() };
                    Store[cid] = cf;
                }
                cf.OwnerID = c["agent_id"].AsUUID();
                cf.Name = c["name"].AsString();
                cf.Version = c["version"].AsInteger();
                cf.PreferredType = (FolderType)(sbyte)c["type_default"].AsInteger();
                ret.Add(cf);
            }
        if (entry["items"] is OSDArray its)
            foreach (var o in its)
            {
                var item = InventoryItem.FromOSD(o);
                Store[item.UUID] = item;
                ret.Add(item);
            }
        return ret;
    }

    /// <summary>The per-folder listing: one request for one folder. Null when the server did
    /// not answer (LibreMetaverse returns an empty list then; the sentinel tells them apart).</summary>
    async Task<List<InventoryBase>?> ListOneAsync(UUID id, CancellationToken ct)
    {
        try
        {
            if (Store.TryGetValue(id, out var b) && b is InventoryFolder sf) sf.DescendentCount = Unanswered;
            var contents = await _client.Inventory.RequestFolderContentsAsync(
                id, Owner, true, true, InventorySortOrder.ByName, ct).ConfigureAwait(false);
            bool answered = Store.TryGetValue(id, out var after) && after is InventoryFolder af
                && af.DescendentCount != Unanswered;
            return answered ? contents : null;
        }
        catch (OperationCanceledException) { throw; }
        catch { return null; }   // reported as not fetched
    }

    /// <summary>Removes the local folders the server did not confirm, with any unconfirmed
    /// folders beneath them. A phantom that is a system folder, or has an item or a system
    /// folder anywhere beneath it, is left alone and reported.</summary>
    (List<string> Removed, List<string> KeptSystem, List<string> KeptWithItems) PrunePhantoms(
        IEnumerable<InventoryFolder> localKids, HashSet<string> confirmed)
    {
        var removed = new List<string>();
        var keptSystem = new List<string>();
        var keptItems = new List<string>();
        var byId = localKids.GroupBy(f => f.UUID).Select(g => g.First()).ToDictionary(f => f.UUID.ToString());
        var phantoms = Phantoms.Find(byId.Values.Select(Info), confirmed);
        foreach (var p in phantoms)
        {
            if (!byId.TryGetValue(p.Id, out var folder)) continue;
            if (!Phantoms.Prunable(p)) { keptSystem.Add($"{p.Name} ({p.Kind})"); continue; }
            try
            {
                var subtree = new List<InventoryFolder>();
                bool blocked = false, hasItems = false;
                var stack = new Stack<InventoryFolder>();
                stack.Push(folder);
                var seen = new HashSet<UUID>();
                while (stack.Count > 0 && subtree.Count < 100000)
                {
                    var cur = stack.Pop();
                    if (!seen.Add(cur.UUID)) continue;
                    subtree.Add(cur);
                    foreach (var c in Store.GetContents(cur.UUID))
                    {
                        if (c is InventoryFolder cf)
                        {
                            if (confirmed.Contains(cf.UUID.ToString()) || !Phantoms.Prunable(Info(cf))) blocked = true;
                            else stack.Push(cf);
                        }
                        else hasItems = true;
                    }
                }
                if (hasItems || blocked) { keptItems.Add(p.Name); continue; }
                // Deepest first, so no node is removed while it still has children.
                for (int i = subtree.Count - 1; i >= 0; i--) Store.RemoveNodeFor(subtree[i]);
                removed.Add(p.Name);
            }
            catch { keptItems.Add(p.Name + " (could not remove)"); }
        }
        return (removed, keptSystem, keptItems);
    }

    static FolderInfo Info(InventoryFolder f) =>
        new(f.UUID.ToString(), f.Name, f.ParentUUID.ToString(), f.PreferredType.ToString());

    /// <summary>Asks the server whether each of these folders is where the local store says it
    /// is (listed among its parent's child folders). Returns the names of those that are not.</summary>
    public async Task<List<string>> UnconfirmedAsync(IEnumerable<UUID> folders, CancellationToken ct)
    {
        var bad = new List<string>();
        var rootId = Store.RootFolder?.UUID;
        foreach (var group in folders.Distinct()
            .Where(f => f != rootId)
            .GroupBy(f => Store.TryGetValue(f, out var b) ? b.ParentUUID : UUID.Zero))
        {
            ct.ThrowIfCancellationRequested();
            var kids = group.Key == UUID.Zero
                ? new List<InventoryFolder>()
                : await ChildFoldersAsync(group.Key, ct).ConfigureAwait(false);
            var there = kids.Select(k => k.UUID).ToHashSet();
            foreach (var f in group.Where(f => !there.Contains(f)))
                bad.Add(Store.TryGetValue(f, out var b) ? b.Name : f.ToString());
        }
        return bad;
    }

    /// <summary>True when the inventory service (AISv3) is available on this region.</summary>
    bool UseAis => _client.AisClient?.IsAvailable == true;

    /// <summary>Calls <paramref name="work"/> for every item, at most <paramref name="limit"/> at once,
    /// and returns the results in item order.</summary>
    static async Task<bool[]> RunLimitedAsync<T>(IReadOnlyList<T> items, int limit,
        Func<T, Task<bool>> work, CancellationToken ct)
    {
        var results = new bool[items.Count];
        using var gate = new SemaphoreSlim(limit);
        await Task.WhenAll(items.Select(async (item, i) =>
        {
            await gate.WaitAsync(ct).ConfigureAwait(false);
            try { results[i] = await work(item).ConfigureAwait(false); }
            catch (OperationCanceledException) { throw; }
            catch { results[i] = false; }
            finally { gate.Release(); }
        })).ConfigureAwait(false);
        return results;
    }

    /// <summary>The server's view of a folder's child folders. A failed fetch gives an empty list.</summary>
    async Task<List<InventoryFolder>> ChildFoldersAsync(UUID parent, CancellationToken ct)
    {
        try
        {
            var contents = await _client.Inventory.RequestFolderContentsAsync(
                parent, Owner, true, false, InventorySortOrder.ByName, ct).ConfigureAwait(false);
            return contents.OfType<InventoryFolder>().ToList();
        }
        catch (OperationCanceledException) { throw; }
        catch { return new List<InventoryFolder>(); }
    }

    /// <summary>Polls the parent's contents on the server until the folder shows up (by id when
    /// known, else by name). Null if it never does.</summary>
    async Task<UUID?> WaitForFolderAsync(UUID parent, UUID? id, string name, CancellationToken ct)
    {
        var deadline = DateTime.UtcNow + ConfirmTimeout;
        while (true)
        {
            ct.ThrowIfCancellationRequested();
            var kids = await ChildFoldersAsync(parent, ct).ConfigureAwait(false);
            var hit = id.HasValue ? kids.FirstOrDefault(f => f.UUID == id.Value)
                                  : kids.FirstOrDefault(f => f.Name == name);
            if (hit != null) return hit.UUID;
            if (DateTime.UtcNow >= deadline) return null;
            await Task.Delay(ConfirmPoll, ct).ConfigureAwait(false);
        }
    }

    /// <summary>POSTs a new category to the inventory service. The payload is the AISv3
    /// create-category body the viewer sends: {"categories": [{"name": ..., "type_default": -1}]}.
    /// Done here rather than through InventoryAISClient.CreateInventoryAsync because that method
    /// expects an "items" block in the reply (so it reports failure for a category create) and
    /// never returns the new id. The id comes from "_created_categories" in the reply.</summary>
    async Task<(bool Ok, UUID? Id)> AisCreateCategoryAsync(UUID parent, string name, CancellationToken ct)
    {
        try
        {
            var cap = _client.Network.CurrentSim?.Caps?.CapabilityURI(InventoryAISClient.INVENTORY_CAP_NAME);
            if (cap == null) return (false, null);
            var payload = new OSDMap
            {
                ["categories"] = new OSDArray
                {
                    new OSDMap { ["name"] = OSD.FromString(name), ["type_default"] = OSD.FromInteger(-1) },
                },
            };
            var uri = new Uri($"{cap}/category/{parent}?tid={UUID.Random()}");
            using var content = new StringContent(OSDParser.SerializeLLSDXmlString(payload), Encoding.UTF8, HttpCapsClient.LLSD_XML);
            using var reply = await _client.HttpCapsClient.PostAsync(uri, content, ct).ConfigureAwait(false);
            if (!reply.IsSuccessStatusCode)
            {
                _say($"the inventory service refused to create '{name}': {(int)reply.StatusCode} {reply.ReasonPhrase}");
                return (false, null);
            }
            UUID? id = null;
            try
            {
                var body = await reply.Content.ReadAsStreamAsync(ct).ConfigureAwait(false);
                if (OSDParser.Deserialize(body) is OSDMap map
                    && map.TryGetValue("_created_categories", out var created) && created is OSDArray arr && arr.Count > 0)
                {
                    var u = arr[0].AsUUID();
                    if (u != UUID.Zero) id = u;
                }
            }
            catch (OperationCanceledException) { throw; }
            catch { /* created, but no readable id: found by name below */ }
            return (true, id);
        }
        catch (OperationCanceledException) { throw; }
        catch (Exception ex)
        {
            _say($"creating '{name}' failed: {ex.Message}");
            return (false, null);
        }
    }

    /// <summary>Makes one folder and returns its id once the server confirms it exists.
    /// Reuses a same-named child that is already there. Throws FolderNotConfirmedException
    /// if confirmation never comes.</summary>
    async Task<(UUID Id, bool Created)> CreateConfirmedAsync(UUID parent, string name, CancellationToken ct)
    {
        var kids = await ChildFoldersAsync(parent, ct).ConfigureAwait(false);
        if (kids.FirstOrDefault(f => f.Name == name) is { } existing) return (existing.UUID, false);

        if (UseAis)
        {
            for (int attempt = 0; attempt < 2; attempt++)
            {
                var (ok, id) = await AisCreateCategoryAsync(parent, name, ct).ConfigureAwait(false);
                if (ok)
                {
                    var seen = await WaitForFolderAsync(parent, id, name, ct).ConfigureAwait(false);
                    if (seen != null) return (seen.Value, true);
                    break;   // accepted but never appeared: do not post a second copy
                }
                if (attempt == 0)
                {
                    await Task.Delay(RetryPause, ct).ConfigureAwait(false);
                    // The refusal may have been a timeout after the folder was made.
                    var late = (await ChildFoldersAsync(parent, ct).ConfigureAwait(false)).FirstOrDefault(f => f.Name == name);
                    if (late != null) return (late.UUID, true);
                }
            }
            throw new FolderNotConfirmedException(name);
        }

        var newId = _client.Inventory.CreateFolder(parent, name);
        var confirmed = await WaitForFolderAsync(parent, newId, name, ct).ConfigureAwait(false);
        if (confirmed == null) throw new FolderNotConfirmedException(name);
        return (confirmed.Value, true);
    }

    /// <summary>How many folders the last CreateFoldersAsync really made (not reused).</summary>
    public int CreatedCount { get; private set; }

    /// <summary>Creates the missing destination folders, parents first, and journals each one
    /// with its confirmed id. A folder the server never confirms raises
    /// FolderNotConfirmedException, which stops the apply before anything moves into it.
    /// Returns path -> folder id.</summary>
    public async Task<Dictionary<string, UUID>> CreateFoldersAsync(
        IEnumerable<string> paths, IFolderTree tree, Journal journal, CancellationToken ct)
    {
        var made = new Dictionary<string, UUID>();
        CreatedCount = 0;
        foreach (var path in paths)
        {
            ct.ThrowIfCancellationRequested();
            var parts = PlanReader.SplitPath(path);
            var parentPath = string.Join(PlanReader.PathSeparator, parts.Take(parts.Length - 1));
            UUID parent;
            if (parentPath.Length == 0) parent = Store.RootFolder!.UUID;
            else if (made.TryGetValue(parentPath, out var p)) parent = p;
            else if (ResolvePath(tree, parentPath) is { } existing) parent = existing;
            else throw new InvalidOperationException($"cannot create {path}: its parent {parentPath} does not exist");

            if (ResolvePath(tree, path) is { } already) { made[path] = already; continue; }

            var name = parts[^1];
            var (id, created) = await CreateConfirmedAsync(parent, name, ct).ConfigureAwait(false);
            if (created)
            {
                journal.Write(new JournalEntry(DateTime.UtcNow, JournalKind.Create, id.ToString(), "", parent.ToString(), name));
                CreatedCount++;
            }
            made[path] = id;
            await Task.Delay(CreatePause, ct).ConfigureAwait(false);
        }
        return made;
    }

    void StoreSetParent(UUID folder, UUID parent)
    {
        try
        {
            if (Store.TryGetValue(folder, out var b) && b is InventoryFolder f && f.ParentUUID != parent)
            {
                f.ParentUUID = parent;
                Store.UpdateNodeFor(f);
            }
        }
        catch { /* the refetch afterwards corrects the store anyway */ }
    }

    /// <summary>Sends one MoveInventoryFolder message (the viewer's own folder move; up to
    /// MoveBatch blocks). The inventory service's PATCH parent_id is not used: the server
    /// answers it 400 Bad Request. The message has no reply.</summary>
    void SendMoveMessage(IEnumerable<(UUID Folder, UUID From, UUID To, string Name)> chunk)
    {
        var blocks = chunk.Select(m => new MoveInventoryFolderPacket.InventoryDataBlock
        {
            FolderID = m.Folder,
            ParentID = m.To,
        }).ToArray();
        var packet = new MoveInventoryFolderPacket
        {
            AgentData =
            {
                AgentID = _client.Self.AgentID,
                SessionID = _client.Self.SessionID,
                Stamp = false,
            },
            InventoryData = blocks,
        };
        _client.Network.SendPacket(packet);
    }

    /// <summary>Lists each destination on the server (polling every MovePoll for up to
    /// MoveConfirmTimeout) until every move's folder shows there; returns the moves that never
    /// did. Only the destinations of still-missing moves are asked again.</summary>
    async Task<List<(UUID Folder, UUID From, UUID To, string Name)>> WaitForMovesAsync(
        List<(UUID Folder, UUID From, UUID To, string Name)> pending, CancellationToken ct)
    {
        var deadline = DateTime.UtcNow + MoveConfirmTimeout;
        while (true)
        {
            ct.ThrowIfCancellationRequested();
            var dests = pending.Select(m => m.To).Distinct().ToList();
            var listed = new Dictionary<string, HashSet<string>>();
            var gate = new object();
            await RunLimitedAsync(dests, FetchConcurrency, async d =>
            {
                var kids = await ChildFoldersAsync(d, ct).ConfigureAwait(false);
                lock (gate) listed[d.ToString()] = kids.Select(k => k.UUID.ToString()).ToHashSet();
                return true;
            }, ct).ConfigureAwait(false);
            pending = MoveBatches.NotThere(pending, m => m.Folder.ToString(), m => m.To.ToString(), listed);
            if (pending.Count == 0 || DateTime.UtcNow >= deadline) return pending;
            await Task.Delay(MovePoll, ct).ConfigureAwait(false);
        }
    }

    /// <summary>Moves folders with the viewer's MoveInventoryFolder message, 20 per message,
    /// journalling each batch before it is sent. The message gets no reply, so afterwards the
    /// server is asked for each destination's contents (every 2 s, up to 20 s). Folders still
    /// missing are resent once and confirmed again; the ones that still are not there are
    /// returned as failed. The local store is not touched here: the reconcile step sets
    /// parents from what the server reports.</summary>
    public async Task<(List<(UUID Folder, UUID From, UUID To, string Name)> Moved,
                       List<(UUID Folder, UUID From, UUID To, string Name)> Failed)> MoveAsync(
        IReadOnlyList<(UUID Folder, UUID From, UUID To, string Name)> moves, Journal journal, CancellationToken ct)
    {
        int sent = 0;
        foreach (var chunk in MoveBatches.Split(moves, MoveBatch))
        {
            ct.ThrowIfCancellationRequested();
            foreach (var m in chunk)
                journal.Write(new JournalEntry(DateTime.UtcNow, JournalKind.Move,
                    m.Folder.ToString(), m.From.ToString(), m.To.ToString(), m.Name));
            SendMoveMessage(chunk);
            sent += chunk.Length;
            _say($"sent {sent} of {moves.Count} moves");
            if (sent < moves.Count) await Task.Delay(MovePause, ct).ConfigureAwait(false);
        }
        if (moves.Count == 0) return (new(), new());

        _say("asking the server whether the moves landed");
        var missing = await WaitForMovesAsync(moves.ToList(), ct).ConfigureAwait(false);
        if (missing.Count > 0)
        {
            _say($"{missing.Count} moves have not shown up; sending them once more");
            foreach (var chunk in MoveBatches.Split(missing, MoveBatch))
            {
                ct.ThrowIfCancellationRequested();
                SendMoveMessage(chunk);
                await Task.Delay(MovePause, ct).ConfigureAwait(false);
            }
            missing = await WaitForMovesAsync(missing, ct).ConfigureAwait(false);
        }
        var moved = moves.Where(m => !missing.Contains(m)).ToList();
        _say($"moved {moved.Count} of {moves.Count}" + (missing.Count > 0 ? $", {missing.Count} failed" : ""));
        return (moved, missing);
    }

    /// <summary>The server's view of a folder's items. Asked the way the fetch asks (folders and items), which is known to return both. A failed fetch gives an empty list.</summary>
    async Task<List<InventoryItem>> ItemsInAsync(UUID folder, CancellationToken ct)
    {
        try
        {
            var contents = await _client.Inventory.RequestFolderContentsAsync(
                folder, Owner, true, true, InventorySortOrder.ByName, ct).ConfigureAwait(false);
            return contents.OfType<InventoryItem>().ToList();
        }
        catch (OperationCanceledException) { throw; }
        catch { return new List<InventoryItem>(); }
    }

    /// <summary>Sends one MoveInventoryItem message (the viewer's own item move; up to ItemMoveBatch
    /// blocks). The message has no reply. An empty NewName leaves the item's name alone.</summary>
    void SendItemMoveMessage(IEnumerable<(UUID Item, UUID From, UUID To, string Name)> chunk)
    {
        var blocks = chunk.Select(m => new MoveInventoryItemPacket.InventoryDataBlock
        {
            ItemID = m.Item,
            FolderID = m.To,
            NewName = Array.Empty<byte>(),
        }).ToArray();
        var packet = new MoveInventoryItemPacket
        {
            AgentData =
            {
                AgentID = _client.Self.AgentID,
                SessionID = _client.Self.SessionID,
                Stamp = false,
            },
            InventoryData = blocks,
        };
        _client.Network.SendPacket(packet);
    }

    /// <summary>Lists each destination on the server, items included (polling every MovePoll for up
    /// to MoveConfirmTimeout), until every item shows there; returns the moves that never did.</summary>
    async Task<List<(UUID Item, UUID From, UUID To, string Name)>> WaitForItemMovesAsync(
        List<(UUID Item, UUID From, UUID To, string Name)> pending, CancellationToken ct)
    {
        var deadline = DateTime.UtcNow + MoveConfirmTimeout;
        while (true)
        {
            ct.ThrowIfCancellationRequested();
            var dests = pending.Select(m => m.To).Distinct().ToList();
            var listed = new Dictionary<string, HashSet<string>>();
            var gate = new object();
            await RunLimitedAsync(dests, FetchConcurrency, async d =>
            {
                var there = await ItemsInAsync(d, ct).ConfigureAwait(false);
                lock (gate) listed[d.ToString()] = there.Select(k => k.UUID.ToString()).ToHashSet();
                return true;
            }, ct).ConfigureAwait(false);
            pending = MoveBatches.NotThere(pending, m => m.Item.ToString(), m => m.To.ToString(), listed);
            if (pending.Count == 0 || DateTime.UtcNow >= deadline) return pending;
            await Task.Delay(MovePoll, ct).ConfigureAwait(false);
        }
    }

    /// <summary>Moves items (into folders, or into Trash) with the viewer's MoveInventoryItem
    /// message, 30 per message, journalling each batch before it is sent. The message gets no
    /// reply, so afterwards the server is asked for each destination's items (every 2 s, up to
    /// 20 s). Items still missing are resent once and confirmed again; the ones that still are
    /// not there are returned as failed. Progress is reported every 30 s. The local store is
    /// updated for the confirmed items only.</summary>
    public async Task<(List<(UUID Item, UUID From, UUID To, string Name)> Moved,
                       List<(UUID Item, UUID From, UUID To, string Name)> Failed)> MoveItemsAsync(
        IReadOnlyList<(UUID Item, UUID From, UUID To, string Name)> moves, Journal journal, CancellationToken ct)
    {
        if (moves.Count == 0) return (new(), new());
        int sent = 0;
        var clock = Stopwatch.StartNew();
        var lastReport = TimeSpan.Zero;
        foreach (var chunk in MoveBatches.Split(moves, ItemMoveBatch))
        {
            ct.ThrowIfCancellationRequested();
            foreach (var m in chunk)
                journal.Write(new JournalEntry(DateTime.UtcNow, JournalKind.Item,
                    m.Item.ToString(), m.From.ToString(), m.To.ToString(), m.Name));
            SendItemMoveMessage(chunk);
            sent += chunk.Length;
            if (clock.Elapsed - lastReport >= ItemProgress)
            {
                lastReport = clock.Elapsed;
                _say($"sent {sent} of {moves.Count} item moves");
            }
            if (sent < moves.Count) await Task.Delay(MovePause, ct).ConfigureAwait(false);
        }
        _say($"sent {sent} of {moves.Count} item moves; asking the server whether they landed");

        var missing = await WaitForItemMovesAsync(moves.ToList(), ct).ConfigureAwait(false);
        if (missing.Count > 0)
        {
            _say($"{missing.Count} item moves have not shown up; sending them once more");
            foreach (var chunk in MoveBatches.Split(missing, ItemMoveBatch))
            {
                ct.ThrowIfCancellationRequested();
                SendItemMoveMessage(chunk);
                await Task.Delay(MovePause, ct).ConfigureAwait(false);
            }
            missing = await WaitForItemMovesAsync(missing, ct).ConfigureAwait(false);
        }
        var moved = moves.Where(m => !missing.Contains(m)).ToList();
        foreach (var m in moved) StoreSetItemParent(m.Item, m.To);
        _say($"moved {moved.Count} of {moves.Count} items" + (missing.Count > 0 ? $", {missing.Count} failed" : ""));
        return (moved, missing);
    }

    void StoreSetItemParent(UUID item, UUID parent)
    {
        try
        {
            if (Store.TryGetValue(item, out var b) && b is InventoryItem i && i.ParentUUID != parent)
            {
                i.ParentUUID = parent;
                Store.UpdateNodeFor(i);
            }
        }
        catch { /* the next fetch corrects the store anyway */ }
    }

    /// <summary>Asks the server for each destination's items and returns the moves whose item is not there.</summary>
    public async Task<List<(UUID Item, UUID To, string Name)>> VerifyItemsAsync(
        IEnumerable<(UUID Item, UUID From, UUID To, string Name)> moves, CancellationToken ct)
    {
        var missing = new List<(UUID, UUID, string)>();
        foreach (var group in moves.GroupBy(m => m.To))
        {
            ct.ThrowIfCancellationRequested();
            var there = (await ItemsInAsync(group.Key, ct).ConfigureAwait(false)).Select(i => i.UUID).ToHashSet();
            foreach (var m in group)
                if (!there.Contains(m.Item)) missing.Add((m.Item, m.To, m.Name));
        }
        return missing;
    }

    /// <summary>Renames folders, journalling each one before it is sent. The parent
    /// is the folder's parent after any move. With the inventory service a refused rename
    /// is retried once; the ones that still fail are returned.</summary>
    public async Task<(List<(UUID Folder, UUID Parent, string OldName, string NewName)> Renamed,
                       List<(UUID Folder, UUID Parent, string OldName, string NewName)> Failed)> RenameAsync(
        IReadOnlyList<(UUID Folder, UUID Parent, string OldName, string NewName)> renames,
        Journal journal, CancellationToken ct)
    {
        var renamed = new List<(UUID Folder, UUID Parent, string OldName, string NewName)>();
        var failed = new List<(UUID Folder, UUID Parent, string OldName, string NewName)>();
        foreach (var r in renames)
        {
            ct.ThrowIfCancellationRequested();
            journal.Write(new JournalEntry(DateTime.UtcNow, JournalKind.Rename, r.Folder.ToString(),
                r.Parent.ToString(), r.Parent.ToString(), r.NewName, r.OldName));
            bool ok = true;
            if (UseAis)
            {
                var update = new OSDMap { ["name"] = OSD.FromString(r.NewName) };
                ok = await _client.AisClient.UpdateCategoryAsync(r.Folder, update, ct).ConfigureAwait(false);
                if (!ok)
                {
                    await Task.Delay(RetryPause, ct).ConfigureAwait(false);
                    ok = await _client.AisClient.UpdateCategoryAsync(r.Folder, update, ct).ConfigureAwait(false);
                }
                if (ok)
                {
                    try
                    {
                        if (Store.TryGetValue(r.Folder, out var b) && b is InventoryFolder f)
                        {
                            f.Name = r.NewName;
                            Store.UpdateNodeFor(f);
                        }
                    }
                    catch { /* the refetch afterwards corrects the store anyway */ }
                }
            }
            else
                _client.Inventory.UpdateFolderProperties(r.Folder, r.Parent, r.NewName, FolderType.None);
            (ok ? renamed : failed).Add(r);
            await Task.Delay(CreatePause, ct).ConfigureAwait(false);
        }
        if (renames.Count > 0)
            _say($"renamed {renamed.Count} folders" + (failed.Count > 0 ? $", {failed.Count} failed" : ""));
        return (renamed, failed);
    }

    /// <summary>Refetches, from the server, every folder the apply touched (old and new parents,
    /// rename parents), then puts each moved folder in the local store under the parent the
    /// server reports. A move the server did not take is shown where it really is, so the
    /// store and the Veles inventory view match reality, including after failed moves.</summary>
    public async Task ReconcileAsync(
        IReadOnlyList<(UUID Folder, UUID From, UUID To, string Name)> moves,
        IReadOnlyList<(UUID Folder, UUID Parent, string OldName, string NewName)> renames, CancellationToken ct)
    {
        var parents = moves.SelectMany(m => new[] { m.From, m.To })
            .Concat(renames.Select(r => r.Parent)).Distinct().ToList();
        if (parents.Count == 0) return;
        _say($"refreshing {parents.Count} folders from the server");
        var seen = new Dictionary<UUID, HashSet<UUID>>();
        var gate = new object();
        await RunLimitedAsync(parents, FetchConcurrency, async id =>
        {
            var kids = await ChildFoldersAsync(id, ct).ConfigureAwait(false);
            lock (gate) seen[id] = kids.Select(k => k.UUID).ToHashSet();
            return true;
        }, ct).ConfigureAwait(false);

        foreach (var m in moves)
        {
            // A failed fetch comes back empty, so only trust a listing that has something in it.
            if (!seen.TryGetValue(m.To, out var there) || there.Count == 0) continue;
            StoreSetParent(m.Folder, there.Contains(m.Folder) ? m.To : m.From);
        }
    }

    /// <summary>Asks the server for each folder's parent and returns the renames whose
    /// folder does not have its new name there.</summary>
    public async Task<List<string>> VerifyRenamesAsync(
        IEnumerable<(UUID Folder, UUID Parent, string OldName, string NewName)> renames, CancellationToken ct)
    {
        var wrong = new List<string>();
        foreach (var group in renames.GroupBy(r => r.Parent))
        {
            ct.ThrowIfCancellationRequested();
            List<InventoryBase>? contents = null;
            try
            {
                contents = await _client.Inventory.RequestFolderContentsAsync(
                    group.Key, Owner, true, false, InventorySortOrder.ByName, ct).ConfigureAwait(false);
            }
            catch (OperationCanceledException) { throw; }
            catch { /* treated as all wrong */ }
            foreach (var r in group)
            {
                var f = contents?.FirstOrDefault(c => c.UUID == r.Folder);
                if (f == null || f.Name != r.NewName) wrong.Add(r.OldName + " -> " + r.NewName);
            }
        }
        return wrong;
    }

    /// <summary>Asks the server for each destination's contents and returns the moves
    /// whose folder is not there.</summary>
    public async Task<List<(UUID Folder, UUID To, string Name)>> VerifyAsync(
        IEnumerable<(UUID Folder, UUID From, UUID To, string Name)> moves, CancellationToken ct)
    {
        var missing = new List<(UUID, UUID, string)>();
        foreach (var group in moves.GroupBy(m => m.To))
        {
            ct.ThrowIfCancellationRequested();
            List<InventoryBase>? contents = null;
            try
            {
                contents = await _client.Inventory.RequestFolderContentsAsync(
                    group.Key, Owner, true, false, InventorySortOrder.ByName, ct).ConfigureAwait(false);
            }
            catch (OperationCanceledException) { throw; }
            catch { /* treated as all missing */ }
            var there = new HashSet<UUID>(contents?.Select(c => c.UUID) ?? Enumerable.Empty<UUID>());
            foreach (var m in group)
                if (!there.Contains(m.Folder)) missing.Add((m.Folder, m.To, m.Name));
        }
        return missing;
    }

    public static UUID? ResolvePath(IFolderTree tree, string path)
    {
        var cur = tree.Get(tree.RootId);
        foreach (var part in PlanReader.SplitPath(path))
        {
            if (cur == null) return null;
            cur = tree.Child(cur.Id, part);
        }
        return cur != null && UUID.TryParse(cur.Id, out var id) ? id : null;
    }
}
