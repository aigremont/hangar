using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;

namespace Veles.Plugin.Hangar.Core;

/// <summary>Trash is for the action plan: the item or folder goes to the Trash folder.</summary>
public enum Outcome { Move, AlreadyThere, Skip, Trash }

/// <summary>What `check` needs from either kind of checked plan.</summary>
public interface ICheckedPlan
{
    string Summary();
    void WritePreview(TextWriter w);

    /// <summary>The reason of every skipped row.</summary>
    IEnumerable<string> SkipReasons { get; }
}

/// <summary>What will happen to one plan row, decided against the live inventory.</summary>
public sealed record CheckedRow(PlanRow Row, Outcome Outcome, string Reason,
    string? CurrentParentId = null, string? DestinationId = null, string? Rename = null);

/// <summary>The plan checked against the live inventory: nothing in it has moved yet.</summary>
public sealed class CheckedPlan : ICheckedPlan
{
    public List<CheckedRow> Rows { get; } = new();

    /// <summary>Destination paths that do not exist yet, parents before children.</summary>
    public List<string> FoldersToCreate { get; } = new();

    public IEnumerable<CheckedRow> Moves => Rows.Where(r => r.Outcome == Outcome.Move);
    public IEnumerable<CheckedRow> Skips => Rows.Where(r => r.Outcome == Outcome.Skip);
    public IEnumerable<string> SkipReasons => Skips.Select(s => s.Reason);

    public string Summary() =>
        $"{Moves.Count()} folder moves ({Moves.Sum(r => r.Row.Items)} items), " +
        $"{FoldersToCreate.Count} new folders, " +
        (Moves.Count(r => r.Rename != null) is var n && n > 0 ? $"{n} renames, " : "") +
        $"{Rows.Count(r => r.Outcome == Outcome.AlreadyThere)} already in place, " +
        $"{Skips.Count()} skipped";

    public void WritePreview(TextWriter w)
    {
        w.WriteLine(Csv.Line("outcome", "product_folder", "items", "to", "reason", "folder_id"));
        foreach (var r in Rows)
            w.WriteLine(Csv.Line(r.Outcome, r.Row.ProductFolder, r.Row.Items, r.Row.To,
                r.Rename == null ? r.Reason : $"{r.Reason}, renamed to {r.Rename}", r.Row.FolderId));
        foreach (var p in FoldersToCreate)
            w.WriteLine(Csv.Line("CreateFolder", "", "", p, "destination does not exist yet", ""));
    }
}

public static class PlanChecker
{
    /// <summary>
    /// Decides each row against the live tree. A row moves only if its folder still
    /// exists, still sits where the plan found it, is not in a protected area, and its
    /// destination resolves (or can be created) outside any protected area and outside
    /// the folder itself.
    /// </summary>
    public static CheckedPlan Check(IEnumerable<PlanRow> rows, IFolderTree tree)
    {
        var plan = new CheckedPlan();
        var toCreate = new List<string>();
        var seenFolders = new HashSet<string>();

        foreach (var row in rows)
        {
            if (!seenFolders.Add(row.FolderId))
            {
                plan.Rows.Add(new CheckedRow(row, Outcome.Skip, "the plan lists this folder twice"));
                continue;
            }
            var folder = string.IsNullOrEmpty(row.FolderId) ? null : tree.Get(row.FolderId);
            if (folder == null)
            {
                plan.Rows.Add(new CheckedRow(row, Outcome.Skip, "no longer in the inventory"));
                continue;
            }
            if (!string.Equals(folder.Kind, "None", StringComparison.OrdinalIgnoreCase))
            {
                plan.Rows.Add(new CheckedRow(row, Outcome.Skip, $"a system folder ({folder.Kind})"));
                continue;
            }

            var parts = PlanReader.SplitPath(row.To);
            if (parts.Length == 0)
            {
                plan.Rows.Add(new CheckedRow(row, Outcome.Skip, "no destination"));
                continue;
            }

            // Resolve the destination as far as it exists.
            var cur = tree.Get(tree.RootId);
            int depth = 0;
            for (; depth < parts.Length && cur != null; depth++)
            {
                var next = tree.Child(cur.Id, parts[depth]);
                if (next == null) break;
                cur = next;
            }
            if (cur == null)
            {
                plan.Rows.Add(new CheckedRow(row, Outcome.Skip, "the inventory root is not loaded"));
                continue;
            }
            bool exists = depth == parts.Length;

            // A rename is due if the plan asks for one and the folder is not called that yet.
            string? rename = row.RenameTo.Length > 0 && folder.Name != row.RenameTo ? row.RenameTo : null;
            if (rename != null && exists && tree.ChildFolders(cur.Id).Any(c => c.Id != folder.Id && c.Name == rename))
            {
                plan.Rows.Add(new CheckedRow(row, Outcome.Skip, $"a folder named {rename} is already there"));
                continue;
            }

            if (folder.ParentId != row.FromId)
            {
                if (exists && folder.ParentId == cur.Id && rename != null)
                    plan.Rows.Add(new CheckedRow(row, Outcome.Move, "in place; rename only", folder.ParentId, cur.Id, rename));
                else if (exists && folder.ParentId == cur.Id)
                    plan.Rows.Add(new CheckedRow(row, Outcome.AlreadyThere, "already in place", folder.ParentId, cur.Id));
                else
                {
                    var now = tree.Get(folder.ParentId);
                    plan.Rows.Add(new CheckedRow(row, Outcome.Skip,
                        "moved since the plan was made (now in " + (now == null ? "?" : tree.PathOf(now)) + ")"));
                }
                continue;
            }

            var why = row.AllowProtected
                ? Exclusions.ReasonAllowingProtectedSource(tree, folder)
                : Exclusions.Reason(tree, folder);
            if (why != null)
            {
                plan.Rows.Add(new CheckedRow(row, Outcome.Skip, why));
                continue;
            }
            // The deepest existing part of the destination must not be protected,
            // nor the folder being moved, nor inside it.
            if (cur.Id != tree.RootId)
            {
                var whyDest = Exclusions.Reason(tree, cur, row.AllowProtected);
                if (whyDest != null)
                {
                    plan.Rows.Add(new CheckedRow(row, Outcome.Skip, "destination " + whyDest));
                    continue;
                }
            }
            if (tree.IsInside(cur, folder.Id))
            {
                plan.Rows.Add(new CheckedRow(row, Outcome.Skip, "destination is inside the folder itself"));
                continue;
            }
            if (exists && cur.Id == folder.ParentId)
            {
                plan.Rows.Add(rename != null
                    ? new CheckedRow(row, Outcome.Move, "in place; rename only", folder.ParentId, cur.Id, rename)
                    : new CheckedRow(row, Outcome.AlreadyThere, "already in place", folder.ParentId, cur.Id));
                continue;
            }
            // Never create a folder under a protected name the user did not ask for.
            if (!exists && !row.AllowProtected && parts.Skip(depth).Any(Exclusions.IsProtectedName))
            {
                plan.Rows.Add(new CheckedRow(row, Outcome.Skip, "destination would create a protected folder"));
                continue;
            }

            if (!exists)
            {
                for (int i = depth + 1; i <= parts.Length; i++)
                {
                    var p = string.Join(PlanReader.PathSeparator, parts.Take(i));
                    if (!toCreate.Contains(p)) toCreate.Add(p);
                }
            }
            plan.Rows.Add(new CheckedRow(row, Outcome.Move,
                exists ? "move" : "move (creates folders)", folder.ParentId, exists ? cur.Id : null, rename));
        }

        // Parents before children.
        plan.FoldersToCreate.AddRange(toCreate.OrderBy(p => PlanReader.SplitPath(p).Length).ThenBy(p => p, StringComparer.Ordinal));
        return plan;
    }
}
