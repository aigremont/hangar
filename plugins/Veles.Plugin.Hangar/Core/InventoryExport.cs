using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;

namespace Veles.Plugin.Hangar.Core;

/// <summary>A folder in the export. Type is a LibreMetaverse FolderType name ("None" for an ordinary folder).</summary>
public sealed record ExportFolder(string Id, string Parent, string Name, string Type);

/// <summary>An item in the export. Masks are LibreMetaverse PermissionMask values as plain integers.</summary>
public sealed record ExportItem(
    string Id, string Parent, string Name, string AssetType, string InvType, string? WearableType,
    string Creator, string Owner, uint Flags, int OwnerMask, int NextOwnerMask,
    bool IsLink, string? LinkTarget);

/// <summary>The inventory as the export sees it: a folder's own record, and its contents.</summary>
public interface IExportSource
{
    string RootId { get; }
    ExportFolder? Folder(string id);
    (IReadOnlyList<ExportFolder> Folders, IReadOnlyList<ExportItem> Items) Contents(string id);
}

public sealed record ExportContent(List<ExportFolder> Folders, List<ExportItem> Items, List<string> CreatorIds);

public static class InventoryExporter
{
    public const string Format = "hangar-export";
    public const int Version = 1;
    const string NullId = "00000000-0000-0000-0000-000000000000";

    /// <summary>Walks from the root through the confirmed folders only. A folder the server did
    /// not confirm is neither listed nor entered; items are taken only from folders that are
    /// listed. The root is always listed. Anything outside the root (the Library) is never seen.</summary>
    public static ExportContent Build(IExportSource source, ISet<string> confirmed)
    {
        var folders = new List<ExportFolder>();
        var items = new List<ExportItem>();
        var seen = new HashSet<string>();
        var queue = new Queue<string>();
        if (source.Folder(source.RootId) is { } root)
        {
            folders.Add(root);
            seen.Add(root.Id);
            queue.Enqueue(root.Id);
        }
        while (queue.Count > 0)
        {
            var (kids, its) = source.Contents(queue.Dequeue());
            items.AddRange(its);
            foreach (var f in kids)
            {
                if (!confirmed.Contains(f.Id) || !seen.Add(f.Id)) continue;
                folders.Add(f);
                queue.Enqueue(f.Id);
            }
        }
        var creators = items.Select(i => i.Creator)
            .Where(c => c.Length > 0 && c != NullId)
            .Distinct().OrderBy(c => c, StringComparer.Ordinal).ToList();
        return new ExportContent(folders, items, creators);
    }

    /// <summary>hangar-export-First_Last-yyyyMMdd-HHmmss.json</summary>
    public static string FileName(string avatarName, DateTime now)
    {
        var bad = Path.GetInvalidFileNameChars();
        var safe = new string(avatarName.Trim().Select(c => char.IsWhiteSpace(c) || bad.Contains(c) ? '_' : c).ToArray());
        if (safe.Length == 0) safe = "avatar";
        return $"hangar-export-{safe}-{now:yyyyMMdd-HHmmss}.json";
    }

    /// <summary>"Legacy Name (display name)"; just the legacy name when the display name adds nothing.</summary>
    public static string FormatCreator(string? first, string? last, string? userName, string? displayName, bool defaultDisplay)
    {
        var legacy = $"{first} {last}".Trim();
        if (legacy.Length == 0) legacy = userName?.Trim() ?? "";
        if (legacy.Length == 0) return "";
        var display = displayName?.Trim() ?? "";
        if (display.Length == 0 || defaultDisplay
            || string.Equals(display, legacy, StringComparison.OrdinalIgnoreCase)
            || string.Equals(display, userName, StringComparison.OrdinalIgnoreCase))
            return legacy;
        return $"{legacy} ({display})";
    }

    public static IEnumerable<List<T>> Batches<T>(IEnumerable<T> source, int size)
    {
        var batch = new List<T>(size);
        foreach (var x in source)
        {
            batch.Add(x);
            if (batch.Count == size) { yield return batch; batch = new List<T>(size); }
        }
        if (batch.Count > 0) yield return batch;
    }

    /// <summary>Compact UTF-8 JSON. Only creators with a name appear in "creators".</summary>
    public static void Write(Stream stream, DateTimeOffset exported, string agentId, string agentName,
        string rootId, ExportContent content, IReadOnlyDictionary<string, string> creators)
    {
        var options = new JsonWriterOptions { Indented = false, Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping };
        using var w = new Utf8JsonWriter(stream, options);
        w.WriteStartObject();
        w.WriteString("format", Format);
        w.WriteNumber("version", Version);
        w.WriteString("exported", exported.ToString("yyyy-MM-dd'T'HH:mm:sszzz"));
        w.WriteString("agent_id", agentId);
        w.WriteString("agent_name", agentName);
        w.WriteString("root_id", rootId);
        w.WriteStartArray("folders");
        foreach (var f in content.Folders)
        {
            w.WriteStartObject();
            w.WriteString("id", f.Id);
            w.WriteString("parent", f.Parent);
            w.WriteString("name", f.Name);
            w.WriteString("type", f.Type);
            w.WriteEndObject();
        }
        w.WriteEndArray();
        w.WriteStartArray("items");
        foreach (var i in content.Items)
        {
            w.WriteStartObject();
            w.WriteString("id", i.Id);
            w.WriteString("parent", i.Parent);
            w.WriteString("name", i.Name);
            w.WriteString("asset_type", i.AssetType);
            w.WriteString("inv_type", i.InvType);
            if (i.WearableType != null) w.WriteString("wearable_type", i.WearableType);
            w.WriteString("creator", i.Creator);
            w.WriteString("owner", i.Owner);
            w.WriteNumber("flags", i.Flags);
            w.WriteStartObject("perms");
            w.WriteNumber("owner", i.OwnerMask);
            w.WriteNumber("next_owner", i.NextOwnerMask);
            w.WriteEndObject();
            w.WriteBoolean("is_link", i.IsLink);
            if (i.IsLink && i.LinkTarget != null) w.WriteString("link_target", i.LinkTarget);
            w.WriteEndObject();
        }
        w.WriteEndArray();
        w.WriteStartObject("creators");
        foreach (var (id, name) in creators.OrderBy(kv => kv.Key, StringComparer.Ordinal))
            w.WriteString(id, name);
        w.WriteEndObject();
        w.WriteEndObject();
    }
}
