using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.RegularExpressions;

namespace Veles.Plugin.Hangar.Core;

/// <summary>A folder as the checks see it. Kind is the folder's preferred type
/// ("None" for an ordinary folder; LibreMetaverse FolderType names otherwise).</summary>
public sealed record FolderInfo(string Id, string Name, string ParentId, string Kind);

/// <summary>The live inventory's folders, as far as the checks need them.</summary>
public interface IFolderTree
{
    string RootId { get; }
    FolderInfo? Get(string id);
    IEnumerable<FolderInfo> ChildFolders(string id);
}

public static class FolderTreeExtensions
{
    /// <summary>The folder's ancestors, nearest first, not including the root.</summary>
    public static IEnumerable<FolderInfo> Ancestors(this IFolderTree tree, FolderInfo f)
    {
        var seen = new HashSet<string>();
        var cur = tree.Get(f.ParentId);
        while (cur != null && cur.Id != tree.RootId && seen.Add(cur.Id))
        {
            yield return cur;
            cur = tree.Get(cur.ParentId);
        }
    }

    /// <summary>"Clothing > Clothes > Alder": names below the root, as the Python tools write them.</summary>
    public static string PathOf(this IFolderTree tree, FolderInfo f)
    {
        if (f.Id == tree.RootId) return "";
        var parts = tree.Ancestors(f).Select(a => a.Name).Reverse().Append(f.Name);
        return string.Join(PlanReader.PathSeparator, parts);
    }

    /// <summary>The Trash system folder (a child of the root); null if the tree has none.</summary>
    public static FolderInfo? TrashFolder(this IFolderTree tree) =>
        tree.ChildFolders(tree.RootId).FirstOrDefault(f => string.Equals(f.Kind, "Trash", StringComparison.OrdinalIgnoreCase));

    /// <summary>True if the folder is the Trash or lies anywhere inside it.</summary>
    public static bool IsInTrash(this IFolderTree tree, FolderInfo f) =>
        new[] { f }.Concat(tree.Ancestors(f)).Any(a => string.Equals(a.Kind, "Trash", StringComparison.OrdinalIgnoreCase));

    public static bool IsInside(this IFolderTree tree, FolderInfo f, string ancestorId) =>
        f.Id == ancestorId || tree.Ancestors(f).Any(a => a.Id == ancestorId);

    /// <summary>The child folder with this name: an exact match first, then ignoring case.
    /// Null if there is none, or if several match ambiguously.</summary>
    public static FolderInfo? Child(this IFolderTree tree, string parentId, string name)
    {
        var kids = tree.ChildFolders(parentId).ToList();
        var exact = kids.Where(k => k.Name == name).ToList();
        if (exact.Count >= 1) return exact[0];
        var loose = kids.Where(k => string.Equals(k.Name.Trim(), name, StringComparison.OrdinalIgnoreCase)).ToList();
        return loose.Count == 1 ? loose[0] : null;
    }
}

/// <summary>Areas Hangar never moves from or into: the same rules as the Python tools.</summary>
public static class FolderContents
{
    /// <summary>True when the folder holds no items and every subfolder (recursively) is also empty,
    /// as far as the fetched tree and item index show.</summary>
    public static bool IsEmptyRecursive(IFolderTree tree, IItemIndex items, string folderId)
    {
        var seen = new HashSet<string>();
        var stack = new Stack<string>();
        stack.Push(folderId);
        while (stack.Count > 0)
        {
            var id = stack.Pop();
            if (!seen.Add(id)) continue;
            if (items.ItemsIn(id).Any()) return false;
            foreach (var c in tree.ChildFolders(id)) stack.Push(c.Id);
        }
        return true;
    }
}

public static class Exclusions
{
    // Folder kinds whose contents are never touched (LibreMetaverse FolderType
    // names; the Python tools' EXCLUDED_PTYPES). Received Items (Inbox) is a
    // source, so it is not here; the Inbox folder itself is still never moved,
    // because no system folder is.
    static readonly HashSet<string> ExcludedKinds = new(StringComparer.OrdinalIgnoreCase)
    {
        "Trash", "CurrentOutfit", "MyOutfits", "Outfit", "MarketplaceListings",
        "MarketplaceStock", "MarketplaceVersion", "Outbox", "LostAndFound", "Favorites",
        "CallingCard", "Landmark",
    };

    // The Python tools' EXCLUDED_NAME_RE: #RLV and its staging folders, fitting
    // queues, redelivery folders.
    static readonly Regex ExcludedName = new(
        @"(^|[^a-z])rlv([^a-z]|$)|wardrobe import|fitting required|needs? fitting|redelivery",
        RegexOptions.IgnoreCase | RegexOptions.Compiled);

    /// <summary>Why this folder, or one above it, is off limits; null if it is not.
    /// <paramref name="allowNamed"/> lifts the name rules (#RLV and the like) for a
    /// destination the user chose deliberately; system areas stay off limits.</summary>
    public static string? Reason(IFolderTree tree, FolderInfo f, bool allowNamed = false)
    {
        foreach (var a in new[] { f }.Concat(tree.Ancestors(f)))
        {
            if (ExcludedKinds.Contains(a.Kind)) return $"inside a {a.Kind} folder ({a.Name})";
            if (!allowNamed && ExcludedName.IsMatch(a.Name)) return $"inside a protected folder ({a.Name})";
        }
        return null;
    }

    // The #RLV part of the name rules alone, and the remaining (never lifted) name rules.
    static readonly Regex RlvOnlyName = new(@"(^|[^a-z])rlv([^a-z]|$)", RegexOptions.IgnoreCase | RegexOptions.Compiled);
    static readonly Regex OtherProtectedName = new(
        @"wardrobe import|fitting required|needs? fitting|redelivery", RegexOptions.IgnoreCase | RegexOptions.Compiled);

    /// <summary>Like <see cref="Reason"/>, but a folder protected only because it is #RLV is
    /// accepted (system areas, fitting queues and redelivery folders still refuse).
    /// <paramref name="insideRlv"/> is true when an #RLV ancestor was passed over.</summary>
    public static string? ReasonAllowingRlv(IFolderTree tree, FolderInfo f, out bool insideRlv)
    {
        insideRlv = false;
        foreach (var a in new[] { f }.Concat(tree.Ancestors(f)))
        {
            if (ExcludedKinds.Contains(a.Kind)) return $"inside a {a.Kind} folder ({a.Name})";
            if (!ExcludedName.IsMatch(a.Name)) continue;
            if (OtherProtectedName.IsMatch(a.Name)) return $"inside a protected folder ({a.Name})";
            insideRlv = true;
        }
        return null;
    }

    /// <summary>
    /// The source rule for a folder-plan row flagged <c>allow_protected</c>: only the name rules for
    /// #RLV, Wardrobe Import and fitting queues are lifted (like <see cref="ReasonAllowingRlv"/> for
    /// items). System kinds, the Trash, redelivery folders and vendor-managed "~" folders (the folder
    /// itself, or an ancestor below an #RLV folder) still refuse.
    /// </summary>
    public static string? ReasonAllowingProtectedSource(IFolderTree tree, FolderInfo f)
    {
        var chain = new[] { f }.Concat(tree.Ancestors(f)).ToList();
        for (int i = 0; i < chain.Count; i++)
        {
            var a = chain[i];
            if (ExcludedKinds.Contains(a.Kind)) return $"inside a {a.Kind} folder ({a.Name})";
            if (Regex.IsMatch(a.Name, @"redelivery", RegexOptions.IgnoreCase))
                return $"inside a protected folder ({a.Name})";
            var n = a.Name.TrimStart();
            if (n.StartsWith("~") || n.StartsWith(".~"))
            {
                bool vendor = i == 0 || chain.Skip(i + 1).Any(x => RlvOnlyName.IsMatch(x.Name));
                if (vendor) return $"inside a vendor-managed folder ({a.Name})";
            }
        }
        return null;
    }

    /// <summary>True if a folder above this one has a protected name (#RLV, Wardrobe Import, a fitting queue).</summary>
    public static bool HasProtectedAncestor(IFolderTree tree, FolderInfo f) =>
        tree.Ancestors(f).Any(a => ExcludedName.IsMatch(a.Name));

    /// <summary>True if a folder with this name would be protected by the name rules.</summary>
    public static bool IsProtectedName(string name) => ExcludedName.IsMatch(name);
}
