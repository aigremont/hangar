// The action plan: item-level actions (move an item, trash an item, trash a folder).
// Core logic: no Veles, Avalonia or LibreMetaverse types, so it is unit-tested on its own.

using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;

namespace Veles.Plugin.Hangar.Core;

public enum ActionKind { ItemMove, ItemTrash, FolderTrash }

/// <summary>One row of an action plan. For a folder_trash row, ItemId is a folder id
/// (and ItemName the folder's name). To is blank for the two trash kinds.</summary>
public sealed record ActionRow(
    ActionKind Kind,
    string ItemName,
    string AssetType,
    string From,
    string To,
    string Because,
    string ItemId,
    string FromId);

public static class ActionPlanReader
{
    public const string Header = "kind,item_name,asset_type,from,to,because,item_id,from_id";

    static readonly string[] Required = { "kind", "item_name", "item_id", "from_id" };

    /// <summary>The kind column's value for each kind, as the plan writes it.</summary>
    public static string KindName(ActionKind kind) => kind switch
    {
        ActionKind.ItemMove => "item_move",
        ActionKind.ItemTrash => "item_trash",
        _ => "folder_trash",
    };

    public static bool TryParseKind(string text, out ActionKind kind)
    {
        switch (text.Trim().ToLowerInvariant())
        {
            case "item_move": kind = ActionKind.ItemMove; return true;
            case "item_trash": kind = ActionKind.ItemTrash; return true;
            case "folder_trash": kind = ActionKind.FolderTrash; return true;
            default: kind = default; return false;
        }
    }

    /// <summary>True if this header row is an action plan's: it has both the kind and item_id columns.</summary>
    public static bool IsActionHeader(IReadOnlyCollection<string> header) =>
        header.Contains("kind") && header.Contains("item_id");

    /// <summary>Reads an action plan (UTF-8, optional BOM, RFC 4180 quoting).</summary>
    public static List<ActionRow> Read(TextReader reader) => FromRecords(Csv.Parse(reader));

    /// <summary>Builds the rows from parsed CSV records, the first being the header. A row with
    /// an unknown kind stops the load: a typo must not be silently left out of the plan.</summary>
    public static List<ActionRow> FromRecords(List<List<string>> records)
    {
        if (records.Count == 0)
            throw new InvalidDataException("the plan is empty");
        var header = PlanReader.HeaderOf(records[0]);
        foreach (var name in Required)
        {
            if (!header.Contains(name))
                throw new InvalidDataException($"the action plan has no '{name}' column; its header is: {Header}");
        }

        string Field(List<string> r, string name)
        {
            int i = header.IndexOf(name);
            return i >= 0 && i < r.Count ? r[i] : "";
        }

        var rows = new List<ActionRow>();
        for (int n = 1; n < records.Count; n++)
        {
            var r = records[n];
            if (r.Count == 1 && r[0].Length == 0) continue;     // blank line
            var kindText = Field(r, "kind");
            if (!TryParseKind(kindText, out var kind))
                throw new InvalidDataException(
                    $"row {n + 1}: unknown kind '{kindText}' (use item_move, item_trash or folder_trash)");
            rows.Add(new ActionRow(kind, Field(r, "item_name"), Field(r, "asset_type"), Field(r, "from"),
                Field(r, "to"), Field(r, "because"), Field(r, "item_id").Trim(), Field(r, "from_id").Trim()));
        }
        return rows;
    }
}

public enum PlanFormat { Folders, Actions }

/// <summary>A plan file after its format was recognised from the header.</summary>
public sealed record LoadedPlan(PlanFormat Format, List<PlanRow> FolderRows, List<ActionRow> ActionRows)
{
    public int Count => Format == PlanFormat.Folders ? FolderRows.Count : ActionRows.Count;

    /// <summary>"12 folder moves" or "40 item actions (30 item moves, 8 item trashes, 2 folder trashes)".</summary>
    public string Describe() => Format == PlanFormat.Folders
        ? $"{FolderRows.Count} folder moves"
        : $"{ActionRows.Count} item actions ({ActionRows.Count(r => r.Kind == ActionKind.ItemMove)} item moves, " +
          $"{ActionRows.Count(r => r.Kind == ActionKind.ItemTrash)} item trashes, " +
          $"{ActionRows.Count(r => r.Kind == ActionKind.FolderTrash)} folder trashes)";
}

public static class PlanFile
{
    /// <summary>Reads either plan format: an action plan has kind and item_id columns, a folder
    /// plan does not.</summary>
    public static LoadedPlan Read(TextReader reader)
    {
        var records = Csv.Parse(reader);
        if (records.Count == 0)
            throw new InvalidDataException("the plan is empty");
        return ActionPlanReader.IsActionHeader(PlanReader.HeaderOf(records[0]))
            ? new LoadedPlan(PlanFormat.Actions, new List<PlanRow>(), ActionPlanReader.FromRecords(records))
            : new LoadedPlan(PlanFormat.Folders, PlanReader.FromRecords(records), new List<ActionRow>());
    }
}

/// <summary>An item as the checks see it. AssetType is the LibreMetaverse AssetType name.</summary>
public sealed record ItemInfo(string Id, string Name, string ParentId, string AssetType);

/// <summary>The live inventory's items, as far as the checks need them.</summary>
public interface IItemIndex
{
    ItemInfo? GetItem(string id);

    /// <summary>The items the server listed directly in this folder.</summary>
    IEnumerable<ItemInfo> ItemsIn(string folderId);
}

/// <summary>Items by id. Built from what the server listed, and kept current as items are moved.</summary>
public sealed class ItemIndex : IItemIndex
{
    readonly Dictionary<string, ItemInfo> _items = new();

    public ItemIndex() { }

    public ItemIndex(IEnumerable<ItemInfo> items)
    {
        foreach (var i in items) _items[i.Id] = i;
    }

    public int Count => _items.Count;

    public ItemInfo? GetItem(string id) => _items.GetValueOrDefault(id);

    public IEnumerable<ItemInfo> ItemsIn(string folderId) => _items.Values.Where(i => i.ParentId == folderId);

    public void SetParent(string id, string parentId)
    {
        if (_items.TryGetValue(id, out var i)) _items[id] = i with { ParentId = parentId };
    }
}
