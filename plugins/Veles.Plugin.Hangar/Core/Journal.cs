using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;

namespace Veles.Plugin.Hangar.Core;

public enum JournalKind { Create, Move, Rename, Item }

/// <summary>One change Hangar made: a folder it created, a folder it moved, or an item it moved.</summary>
/// <remarks>For a Rename, Name is the new name and OldName the previous one;
/// both parent fields hold the folder's parent. For an Item (a move or a trash of one item),
/// FolderId holds the item's id and the parent fields its old and new folder.</remarks>
public sealed record JournalEntry(DateTime At, JournalKind Kind, string FolderId,
    string OldParentId, string NewParentId, string Name, string OldName = "");

/// <summary>
/// Append-only record of every change, written before the change is sent, so an
/// interrupted run can still be undone. Tab-separated, one entry per line.
/// </summary>
public sealed class Journal : IDisposable
{
    readonly StreamWriter _w;

    public string Path { get; }

    public Journal(string path)
    {
        Path = path;
        bool fresh = !File.Exists(path);
        _w = new StreamWriter(new FileStream(path, FileMode.Append, FileAccess.Write, FileShare.Read)) { AutoFlush = true };
        if (fresh) _w.WriteLine("at\tkind\tfolder_id\told_parent\tnew_parent\tname\told_name");
    }

    public void Write(JournalEntry e) =>
        _w.WriteLine(string.Join("\t",
            e.At.ToString("O", CultureInfo.InvariantCulture), e.Kind, e.FolderId,
            e.OldParentId, e.NewParentId, Clean(e.Name), Clean(e.OldName)));

    static string Clean(string s) => s.Replace('\t', ' ').Replace('\n', ' ');

    public void Dispose() => _w.Dispose();

    public static List<JournalEntry> Read(TextReader r)
    {
        var list = new List<JournalEntry>();
        string? line;
        bool header = true;
        while ((line = r.ReadLine()) != null)
        {
            if (header) { header = false; if (line.StartsWith("at\t")) continue; }
            var f = line.Split('\t');
            if (f.Length < 6) continue;
            list.Add(new JournalEntry(
                DateTime.Parse(f[0], CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind),
                Enum.Parse<JournalKind>(f[1]), f[2], f[3], f[4], f[5], f.Length > 6 ? f[6] : ""));
        }
        return list;
    }

    /// <summary>
    /// The moves that undo a journal, newest first: each folder goes back to its old
    /// parent, but only if it is still where Hangar put it and the old parent still
    /// exists. Created folders are reported, not deleted.
    /// </summary>
    public static (List<JournalEntry> Moves, List<string> Skipped, List<JournalEntry> CreatedFolders)
        PlanUndo(IEnumerable<JournalEntry> entries, IFolderTree tree)
    {
        var all = PlanUndoAll(entries, tree);
        return (all.Moves, all.Skipped, all.CreatedFolders);
    }

    /// <summary>As PlanUndo, plus the renames that undo it: each folder gets its old
    /// name back if it still has the name Hangar gave it.</summary>
    public static (List<JournalEntry> Moves, List<JournalEntry> Renames, List<string> Skipped, List<JournalEntry> CreatedFolders)
        PlanUndoAll(IEnumerable<JournalEntry> entries, IFolderTree tree)
    {
        var list = entries.ToList();
        var renames = new List<JournalEntry>();
        var renamed = new HashSet<string>();
        foreach (var e in list.Where(e => e.Kind == JournalKind.Rename).Reverse())
        {
            if (!renamed.Add(e.FolderId)) continue;
            var f = tree.Get(e.FolderId);
            if (f == null || f.Name != e.Name) continue;    // gone, or renamed again since
            renames.Add(e with { Name = e.OldName, OldName = e.Name, OldParentId = f.ParentId, NewParentId = f.ParentId });
        }
        entries = list;
        var moves = new List<JournalEntry>();
        var skipped = new List<string>();
        var done = new HashSet<string>();
        foreach (var e in entries.Where(e => e.Kind == JournalKind.Move).Reverse())
        {
            if (!done.Add(e.FolderId)) continue;     // only the last move of a folder counts
            var f = tree.Get(e.FolderId);
            if (f == null) { skipped.Add($"{e.Name}: no longer in the inventory"); continue; }
            if (f.ParentId != e.NewParentId) { skipped.Add($"{e.Name}: moved again since"); continue; }
            if (tree.Get(e.OldParentId) == null) { skipped.Add($"{e.Name}: its old folder is gone"); continue; }
            moves.Add(e with { OldParentId = e.NewParentId, NewParentId = e.OldParentId });
        }
        var created = entries.Where(e => e.Kind == JournalKind.Create).ToList();
        return (moves, renames, skipped, created);
    }

    /// <summary>
    /// The moves that undo the item entries of a journal, newest first. An item goes back to the
    /// folder it first came from, but only if it is still where Hangar last put it (as the server
    /// listed it) and that old folder still exists. Nothing was deleted, so undoing a trash is
    /// just a move back out of Trash. In the returned entries OldParentId is where the item is
    /// now and NewParentId where it goes.
    /// </summary>
    public static (List<JournalEntry> Moves, List<string> Skipped) PlanUndoItems(
        IEnumerable<JournalEntry> entries, IItemIndex items, IFolderTree tree)
    {
        var moves = new List<(int Last, JournalEntry Entry)>();
        var skipped = new List<string>();
        var groups = entries.Where(e => e.Kind == JournalKind.Item)
            .Select((e, i) => (e, i))
            .GroupBy(x => x.e.FolderId);
        foreach (var g in groups)
        {
            var first = g.First().e;
            var last = g.Last();
            var item = items.GetItem(first.FolderId);
            if (item == null) { skipped.Add($"{first.Name}: no longer in the inventory"); continue; }
            if (item.ParentId != last.e.NewParentId) { skipped.Add($"{first.Name}: moved again since"); continue; }
            if (tree.Get(first.OldParentId) == null) { skipped.Add($"{first.Name}: its old folder is gone"); continue; }
            moves.Add((last.i, last.e with { OldParentId = last.e.NewParentId, NewParentId = first.OldParentId }));
        }
        return (moves.OrderByDescending(m => m.Last).Select(m => m.Entry).ToList(), skipped);
    }
}
