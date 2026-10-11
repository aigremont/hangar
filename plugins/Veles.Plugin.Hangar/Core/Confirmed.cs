using System.Collections.Generic;
using System.Linq;

namespace Veles.Plugin.Hangar.Core;

/// <summary>A view of a folder tree in which only server-confirmed folders exist. A folder
/// the local store holds but the server never returned (a leftover of a fire-and-forget
/// create, say) is invisible, so the checks plan to create it rather than move into it.
/// The root is always confirmed. The set may grow afterwards (folders created this run).</summary>
public sealed class ConfirmedTree : IFolderTree
{
    readonly IFolderTree _inner;
    readonly ISet<string> _confirmed;

    public ConfirmedTree(IFolderTree inner, ISet<string> confirmed)
    {
        _inner = inner;
        _confirmed = confirmed;
    }

    public string RootId => _inner.RootId;

    bool Trusted(string id) => id == _inner.RootId || _confirmed.Contains(id);

    public FolderInfo? Get(string id) => Trusted(id) ? _inner.Get(id) : null;

    public IEnumerable<FolderInfo> ChildFolders(string id) =>
        Trusted(id) ? _inner.ChildFolders(id).Where(f => _confirmed.Contains(f.Id)) : Enumerable.Empty<FolderInfo>();
}

public static class Phantoms
{
    /// <summary>The local folders the server did not confirm.</summary>
    public static List<FolderInfo> Find(IEnumerable<FolderInfo> localFolders, ISet<string> confirmed) =>
        localFolders.Where(f => !confirmed.Contains(f.Id)).ToList();

    /// <summary>Only ordinary folders are ever pruned; system folders are reported, not touched.</summary>
    public static bool Prunable(FolderInfo f) => f.Kind == "None";
}
