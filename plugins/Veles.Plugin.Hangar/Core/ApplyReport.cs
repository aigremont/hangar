using System.Collections.Generic;
using System.Linq;

namespace Veles.Plugin.Hangar.Core;

/// <summary>Pure helpers for retrying failed steps and reporting the outcome in chat.</summary>
public static class ApplyReport
{
    /// <summary>The items whose result was false, in order: what a retry pass should resend.</summary>
    public static List<T> Failed<T>(IReadOnlyList<T> items, IReadOnlyList<bool> results)
    {
        var failed = new List<T>();
        for (int i = 0; i < items.Count; i++)
            if (i >= results.Count || !results[i]) failed.Add(items[i]);
        return failed;
    }

    /// <summary>One chat line naming up to <paramref name="max"/> failures, plus how many more.</summary>
    public static string FailureLine(string what, IReadOnlyList<string> names, int max = 10)
    {
        if (names.Count == 0) return "";
        var shown = string.Join(", ", names.Take(max));
        var more = names.Count > max ? $" (and {names.Count - max} more)" : "";
        return $"{what} failed for {names.Count}: {shown}{more}";
    }

    /// <summary>The final one-line summary of an apply.</summary>
    public static string Summary(int moved, int failed, int renamed, int created) =>
        $"apply finished: moved {moved}, failed {failed}, renamed {renamed}, created {created}";

    /// <summary>The final one-line summary of an action plan apply.</summary>
    public static string ActionSummary(int itemsMoved, int itemsTrashed, int foldersTrashed, int failed, int created) =>
        $"apply finished: moved {itemsMoved} items, trashed {itemsTrashed} items and {foldersTrashed} folders, failed {failed}, created {created}";
}
