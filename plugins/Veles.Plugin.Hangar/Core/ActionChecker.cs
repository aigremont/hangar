using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;

namespace Veles.Plugin.Hangar.Core;

/// <summary>What will happen to one action row, decided against the live inventory.
/// For Move and Trash, DestinationId is the folder it goes to (null when the destination
/// still has to be created); CurrentParentId is where it is now.</summary>
public sealed record CheckedAction(ActionRow Row, Outcome Outcome, string Reason,
    string? CurrentParentId = null, string? DestinationId = null);

/// <summary>The action plan checked against the live inventory: nothing in it has moved yet.</summary>
public sealed class CheckedActionPlan : ICheckedPlan
{
    public List<CheckedAction> Rows { get; } = new();

    /// <summary>Destination paths that do not exist yet, parents before children.</summary>
    public List<string> FoldersToCreate { get; } = new();

    public IEnumerable<CheckedAction> ItemMoves => Rows.Where(r => r.Outcome == Outcome.Move);
    public IEnumerable<CheckedAction> Trashes => Rows.Where(r => r.Outcome == Outcome.Trash);
    public IEnumerable<CheckedAction> Skips => Rows.Where(r => r.Outcome == Outcome.Skip);
    public IEnumerable<string> SkipReasons => Skips.Select(s => s.Reason);

    /// <summary>Everything that will be sent, in plan order (what `apply N` counts).</summary>
    public IEnumerable<CheckedAction> Todo => Rows.Where(r => r.Outcome == Outcome.Move || r.Outcome == Outcome.Trash);

    public string Summary() =>
        $"{ItemMoves.Count()} item moves, " +
        $"{Trashes.Count(r => r.Row.Kind == ActionKind.ItemTrash)} items to Trash, " +
        $"{Trashes.Count(r => r.Row.Kind == ActionKind.FolderTrash)} folders to Trash, " +
        $"{FoldersToCreate.Count} new folders, " +
        $"{Rows.Count(r => r.Outcome == Outcome.AlreadyThere)} already in place, " +
        $"{Skips.Count()} skipped";

    public void WritePreview(TextWriter w)
    {
        w.WriteLine(Csv.Line("outcome", "kind", "item_name", "asset_type", "from", "to", "reason", "item_id"));
        foreach (var r in Rows)
            w.WriteLine(Csv.Line(r.Outcome, ActionPlanReader.KindName(r.Row.Kind), r.Row.ItemName, r.Row.AssetType,
                r.Row.From, r.Outcome == Outcome.Trash ? "Trash" : r.Row.To, r.Reason, r.Row.ItemId));
        foreach (var p in FoldersToCreate)
            w.WriteLine(Csv.Line("CreateFolder", "", "", "", "", p, "destination does not exist yet", ""));
    }
}

public static class ActionChecker
{
    /// <summary>
    /// Decides each row against the live tree and the server's item listing. An item (or folder)
    /// moves only if it still exists, still sits where the plan found it, is not in a protected
    /// area, and its destination resolves (or can be created) outside the system areas. Nothing is
    /// ever deleted: a trash is a move into the Trash folder. Items may be filed into an existing
    /// system folder such as Notecards, and into an existing folder under a protected name (#RLV);
    /// folders are never created inside a protected area, and the action plan has no way to ask for it.
    /// </summary>
    public static CheckedActionPlan Check(IEnumerable<ActionRow> rows, IFolderTree tree, IItemIndex items)
    {
        var plan = new CheckedActionPlan();
        var toCreate = new List<string>();
        var seen = new HashSet<string>();
        var trash = tree.TrashFolder();

        foreach (var row in rows)
        {
            CheckedAction result;
            if (!seen.Add(row.ItemId))
                result = new CheckedAction(row, Outcome.Skip, "the plan lists this " + Noun(row) + " twice");
            else
                result = row.Kind switch
                {
                    ActionKind.ItemMove => CheckItemMove(row, tree, items, toCreate),
                    ActionKind.ItemTrash => CheckItemTrash(row, tree, items, trash),
                    _ => CheckFolderTrash(row, tree, items, trash),
                };
            plan.Rows.Add(result);
        }

        // Parents before children.
        plan.FoldersToCreate.AddRange(toCreate.OrderBy(p => PlanReader.SplitPath(p).Length).ThenBy(p => p, StringComparer.Ordinal));
        return plan;
    }

    static string Noun(ActionRow row) => row.Kind == ActionKind.FolderTrash ? "folder" : "item";

    static string Where(IFolderTree tree, string folderId)
    {
        var now = tree.Get(folderId);
        return now == null ? "?" : tree.PathOf(now);
    }

    // Non-wearable clutter that may leave #RLV product folders.
    static readonly HashSet<string> RlvClutter = new(StringComparer.OrdinalIgnoreCase)
        { "Landmark", "Texture", "Notecard", "Settings" };

    /// <summary>Why an item's source folder is off limits (null if fine), and the asset type when the
    /// item is leaving #RLV. The type is the server's, never the plan row's.</summary>
    static (string? Why, string? RlvType) SourceCheck(ActionRow row, IFolderTree tree, FolderInfo parent, ItemInfo item)
    {
        var why = Exclusions.ReasonAllowingRlv(tree, parent, out bool insideRlv);
        if (why != null) return ("item is " + why, null);
        if (!insideRlv) return (null, null);
        if (!RlvClutter.Contains(item.AssetType))
        {
            var strict = Exclusions.Reason(tree, parent);
            return ("item is " + strict, null);
        }
        if (!string.IsNullOrEmpty(row.AssetType) &&
            !string.Equals(row.AssetType, item.AssetType, StringComparison.OrdinalIgnoreCase))
            return ($"asset type mismatch: the plan says {row.AssetType}, the inventory says {item.AssetType}", null);
        return (null, item.AssetType);
    }

    static CheckedAction CheckItemMove(ActionRow row, IFolderTree tree, IItemIndex items, List<string> toCreate)
    {
        var item = string.IsNullOrEmpty(row.ItemId) ? null : items.GetItem(row.ItemId);
        if (item == null) return new CheckedAction(row, Outcome.Skip, "no longer in the inventory");

        var parts = PlanReader.SplitPath(row.To);
        if (parts.Length == 0) return new CheckedAction(row, Outcome.Skip, "no destination");

        // Resolve the destination as far as it exists.
        var cur = tree.Get(tree.RootId);
        int depth = 0;
        for (; depth < parts.Length && cur != null; depth++)
        {
            var next = tree.Child(cur.Id, parts[depth]);
            if (next == null) break;
            cur = next;
        }
        if (cur == null) return new CheckedAction(row, Outcome.Skip, "the inventory root is not loaded");
        bool exists = depth == parts.Length;

        if (exists && item.ParentId == cur.Id)
            return new CheckedAction(row, Outcome.AlreadyThere, "already in place", item.ParentId, cur.Id);
        if (item.ParentId != row.FromId)
            return new CheckedAction(row, Outcome.Skip,
                "moved since the plan was made (now in " + Where(tree, item.ParentId) + ")");

        var parent = tree.Get(item.ParentId);
        if (parent == null)
            return new CheckedAction(row, Outcome.Skip, "its folder is not known to the server");
        var (why, rlvType) = SourceCheck(row, tree, parent, item);
        if (why != null) return new CheckedAction(row, Outcome.Skip, why);

        // The deepest existing part of the destination must not be a system area. A folder under
        // a protected name (#RLV) is accepted only when the whole destination already exists.
        string reason = exists ? "move" : "move (creates folders)";
        if (rlvType != null) reason = exists ? $"move (out of #RLV: {rlvType})" : $"move (out of #RLV: {rlvType}; creates folders)";
        if (cur.Id != tree.RootId)
        {
            var whyDest = Exclusions.Reason(tree, cur, allowNamed: exists);
            if (whyDest != null) return new CheckedAction(row, Outcome.Skip, "destination " + whyDest);
            if (exists && Exclusions.Reason(tree, cur) != null)
                reason = "move (into an existing protected folder)";
        }
        // Never create a folder under a protected name.
        if (!exists && parts.Skip(depth).Any(Exclusions.IsProtectedName))
            return new CheckedAction(row, Outcome.Skip, "destination would create a protected folder");

        if (!exists)
        {
            for (int i = depth + 1; i <= parts.Length; i++)
            {
                var p = string.Join(PlanReader.PathSeparator, parts.Take(i));
                if (!toCreate.Contains(p)) toCreate.Add(p);
            }
        }
        return new CheckedAction(row, Outcome.Move, reason, item.ParentId, exists ? cur.Id : null);
    }

    static CheckedAction CheckItemTrash(ActionRow row, IFolderTree tree, IItemIndex items, FolderInfo? trash)
    {
        var item = string.IsNullOrEmpty(row.ItemId) ? null : items.GetItem(row.ItemId);
        if (item == null) return new CheckedAction(row, Outcome.Skip, "no longer in the inventory");
        if (trash == null) return new CheckedAction(row, Outcome.Skip, "no Trash folder found");

        var parent = tree.Get(item.ParentId);
        if (item.ParentId == trash.Id || (parent != null && tree.IsInTrash(parent)))
            return new CheckedAction(row, Outcome.AlreadyThere, "already in Trash", item.ParentId, trash.Id);
        if (item.ParentId != row.FromId)
            return new CheckedAction(row, Outcome.Skip,
                "moved since the plan was made (now in " + Where(tree, item.ParentId) + ")");
        if (parent == null)
            return new CheckedAction(row, Outcome.Skip, "its folder is not known to the server");
        var (why, rlvType) = SourceCheck(row, tree, parent, item);
        if (why != null) return new CheckedAction(row, Outcome.Skip, why);
        return new CheckedAction(row, Outcome.Trash, rlvType != null ? $"to Trash (out of #RLV: {rlvType})" : "to Trash", item.ParentId, trash.Id);
    }

    static CheckedAction CheckFolderTrash(ActionRow row, IFolderTree tree, IItemIndex items, FolderInfo? trash)
    {
        var folder = string.IsNullOrEmpty(row.ItemId) ? null : tree.Get(row.ItemId);
        if (folder == null) return new CheckedAction(row, Outcome.Skip, "no longer in the inventory");
        if (!string.Equals(folder.Kind, "None", StringComparison.OrdinalIgnoreCase))
            return new CheckedAction(row, Outcome.Skip, $"a system folder ({folder.Kind})");
        if (trash == null) return new CheckedAction(row, Outcome.Skip, "no Trash folder found");
        if (tree.IsInTrash(folder))
            return new CheckedAction(row, Outcome.AlreadyThere, "already in Trash", folder.ParentId, trash.Id);
        if (folder.ParentId != row.FromId)
            return new CheckedAction(row, Outcome.Skip,
                "moved since the plan was made (now in " + Where(tree, folder.ParentId) + ")");
        var why = Exclusions.Reason(tree, folder);
        if (why == null) return new CheckedAction(row, Outcome.Trash, "to Trash", folder.ParentId, trash.Id);

        // A folder inside #RLV (or Wardrobe Import, a fitting queue) may go when the server shows it
        // empty all the way down. The protected root itself, system kinds, vendor-managed "~" folders
        // and the Trash stay refused.
        if (Exclusions.HasProtectedAncestor(tree, folder) &&
            Exclusions.ReasonAllowingProtectedSource(tree, folder) == null &&
            FolderContents.IsEmptyRecursive(tree, items, folder.Id))
            return new CheckedAction(row, Outcome.Trash, "to Trash (empty, inside #RLV)", folder.ParentId, trash.Id);
        return new CheckedAction(row, Outcome.Skip, why);
    }
}
