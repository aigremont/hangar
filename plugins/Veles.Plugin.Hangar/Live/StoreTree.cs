using System.Collections.Generic;
using System.Linq;
using LibreMetaverse;
using Veles.Plugin.Hangar.Core;

namespace Veles.Plugin.Hangar.Live;

/// <summary>The checks' view of LibreMetaverse's inventory store.</summary>
public sealed class StoreTree : IFolderTree
{
    readonly Inventory _store;

    public StoreTree(Inventory store) => _store = store;

    public string RootId => _store.RootFolder?.UUID.ToString() ?? "";

    public FolderInfo? Get(string id)
    {
        if (!UUID.TryParse(id, out var uuid)) return null;
        return _store.TryGetValue(uuid, out InventoryBase? b) && b is InventoryFolder f ? Info(f) : null;
    }

    public IEnumerable<FolderInfo> ChildFolders(string id)
    {
        if (!UUID.TryParse(id, out var uuid)) return Enumerable.Empty<FolderInfo>();
        List<InventoryBase> contents;
        try { contents = _store.GetContents(uuid); }
        catch { return Enumerable.Empty<FolderInfo>(); }
        return contents.OfType<InventoryFolder>().Select(Info).ToList();
    }

    static FolderInfo Info(InventoryFolder f) =>
        new(f.UUID.ToString(), f.Name, f.ParentUUID.ToString(), f.PreferredType.ToString());
}
