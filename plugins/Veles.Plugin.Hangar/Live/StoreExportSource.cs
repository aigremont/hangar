using System.Collections.Generic;
using System.Linq;
using LibreMetaverse;
using Veles.Plugin.Hangar.Core;

namespace Veles.Plugin.Hangar.Live;

/// <summary>The export's view of LibreMetaverse's inventory store.</summary>
public sealed class StoreExportSource : IExportSource
{
    readonly Inventory _store;

    public StoreExportSource(Inventory store) => _store = store;

    public string RootId => _store.RootFolder?.UUID.ToString() ?? "";

    public ExportFolder? Folder(string id) =>
        UUID.TryParse(id, out var u) && _store.TryGetValue(u, out InventoryBase? b) && b is InventoryFolder f
            ? ToFolder(f) : null;

    public (IReadOnlyList<ExportFolder> Folders, IReadOnlyList<ExportItem> Items) Contents(string id)
    {
        if (!UUID.TryParse(id, out var u)) return (new List<ExportFolder>(), new List<ExportItem>());
        List<InventoryBase> contents;
        try { contents = _store.GetContents(u); }
        catch { return (new List<ExportFolder>(), new List<ExportItem>()); }
        return (contents.OfType<InventoryFolder>().Select(ToFolder).ToList(),
                contents.OfType<InventoryItem>().Select(ToItem).ToList());
    }

    static ExportFolder ToFolder(InventoryFolder f) =>
        new(f.UUID.ToString(), f.ParentUUID.ToString(), f.Name, f.PreferredType.ToString());

    static ExportItem ToItem(InventoryItem i)
    {
        bool link = i.IsLink();
        return new ExportItem(
            i.UUID.ToString(), i.ParentUUID.ToString(), i.Name,
            i.AssetType.ToString(), i.InventoryType.ToString(),
            i is InventoryWearable w ? w.WearableType.ToString() : null,
            i.CreatorID.ToString(), i.OwnerID.ToString(), i.Flags,
            (int)i.Permissions.OwnerMask, (int)i.Permissions.NextOwnerMask,
            link, link ? i.AssetUUID.ToString() : null);
    }
}
