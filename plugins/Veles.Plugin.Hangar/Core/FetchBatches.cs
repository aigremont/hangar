using System;
using System.Collections.Generic;
using System.Linq;

namespace Veles.Plugin.Hangar.Core;

/// <summary>Pure helpers for the batched folder fetch: how many folders go into one request,
/// which requested folders the server did not answer, and whether an answer is complete.</summary>
public static class FetchBatches
{
    /// <summary>How many of <paramref name="queued"/> waiting folders to put in the next request:
    /// an even share per worker (so a short queue still keeps every worker busy), at most
    /// <paramref name="max"/>, at least one. Zero when nothing is queued.</summary>
    public static int NextSize(int queued, int workers, int max)
    {
        if (workers < 1) throw new ArgumentOutOfRangeException(nameof(workers));
        if (max < 1) throw new ArgumentOutOfRangeException(nameof(max));
        if (queued <= 0) return 0;
        return Math.Clamp((queued + workers - 1) / workers, 1, max);
    }

    /// <summary>The requested ids the answer does not contain, in request order.</summary>
    public static List<T> Missing<T>(IEnumerable<T> requested, ISet<T> answered) =>
        requested.Where(id => !answered.Contains(id)).ToList();

    /// <summary>True when a folder's reported descendent count matches the number of child
    /// folders plus items actually listed. A mismatch means the listing may be cut short, so the
    /// folder is listed again on its own.</summary>
    public static bool CountsAgree(int descendents, int folders, int items) =>
        descendents == folders + items;

    /// <summary>A reply is plausible only if it answers at least one requested folder; an
    /// empty or foreign reply to a non-empty request is treated as a failed request.</summary>
    public static bool Plausible(int requested, int answeredOfRequested) =>
        requested == 0 || answeredOfRequested > 0;
}
