using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using LibreMetaverse;
using Veles.Plugin.Hangar.Core;

namespace Veles.Plugin.Hangar.Live;

/// <summary>Resolves avatar UUIDs to "Legacy Name (display name)" through LibreMetaverse's
/// display-name capability (AvatarManager.GetDisplayNamesAsync, 90 ids per request at most),
/// falling back to the legacy UUIDNameReply packets (RequestAvatarNames) when the capability
/// is missing or a batch fails. Batches are paced and the whole lookup has a deadline.</summary>
public sealed class NameLookup
{
    const int BatchSize = 50;
    static readonly TimeSpan BatchPause = TimeSpan.FromMilliseconds(500);
    static readonly TimeSpan LegacyWait = TimeSpan.FromSeconds(8);

    readonly GridClient _client;
    readonly Action<string> _say;

    public NameLookup(GridClient client, Action<string> say)
    {
        _client = client;
        _say = say;
    }

    public async Task<Dictionary<string, string>> ResolveAsync(IReadOnlyList<string> ids, TimeSpan overall, CancellationToken ct)
    {
        var names = new Dictionary<string, string>();
        var todo = ids.Select(s => UUID.TryParse(s, out var u) ? u : UUID.Zero).Where(u => u != UUID.Zero).Distinct().ToList();
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(ct);
        deadline.CancelAfter(overall);
        var token = deadline.Token;
        var av = _client.Avatars;
        bool display = av.DisplayNamesAvailable();
        int done = 0;
        try
        {
            foreach (var batch in InventoryExporter.Batches(todo, BatchSize))
            {
                token.ThrowIfCancellationRequested();
                var pending = batch;
                if (display)
                {
                    try
                    {
                        var (ok, found, _) = await av.GetDisplayNamesAsync(batch, token).ConfigureAwait(false);
                        foreach (var n in found ?? Array.Empty<AgentDisplayName>())
                        {
                            var s = InventoryExporter.FormatCreator(n.LegacyFirstName, n.LegacyLastName,
                                n.UserName, n.DisplayName, n.IsDefaultDisplayName);
                            if (s.Length > 0) names[n.ID.ToString()] = s;
                        }
                        // Ids the service reported as unknown stay unresolved: not an avatar, or gone.
                        pending = ok ? new List<UUID>() : batch.Where(u => !names.ContainsKey(u.ToString())).ToList();
                    }
                    catch (OperationCanceledException) { throw; }
                    catch { pending = batch.Where(u => !names.ContainsKey(u.ToString())).ToList(); }
                }
                if (pending.Count > 0)
                    foreach (var kv in await LegacyAsync(pending, token).ConfigureAwait(false))
                        names[kv.Key.ToString()] = kv.Value;
                done += batch.Count;
                if (todo.Count > BatchSize) _say($"creator names: {done} of {todo.Count} asked");
                await Task.Delay(BatchPause, token).ConfigureAwait(false);
            }
        }
        catch (OperationCanceledException) when (!ct.IsCancellationRequested)
        {
            _say("creator names: timed out; the rest stay unresolved");
        }
        return names;
    }

    async Task<Dictionary<UUID, string>> LegacyAsync(List<UUID> ids, CancellationToken ct)
    {
        var got = new ConcurrentDictionary<UUID, string>();
        var wanted = ids.ToHashSet();
        var all = new TaskCompletionSource();
        void OnReply(object? s, UUIDNameReplyEventArgs e)
        {
            foreach (var (id, name) in e.Names)
                if (wanted.Contains(id)) got[id] = name;
            if (got.Count >= wanted.Count) all.TrySetResult();
        }
        _client.Avatars.UUIDNameReply += OnReply;
        try
        {
            _client.Avatars.RequestAvatarNames(ids);
            await Task.WhenAny(all.Task, Task.Delay(LegacyWait, ct)).ConfigureAwait(false);
            ct.ThrowIfCancellationRequested();
        }
        finally { _client.Avatars.UUIDNameReply -= OnReply; }
        return new Dictionary<UUID, string>(got);
    }
}
