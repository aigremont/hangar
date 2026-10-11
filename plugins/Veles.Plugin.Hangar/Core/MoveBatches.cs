using System;
using System.Collections.Generic;
using System.Linq;

namespace Veles.Plugin.Hangar.Core;

/// <summary>Pure helpers for the UDP folder move: how moves are split into messages, and which
/// ones the server's answer shows did not land. The move message has no reply, so the only
/// proof is listing the destination folder.</summary>
public static class MoveBatches
{
    /// <summary>Splits moves into messages of at most <paramref name="size"/> blocks, in order.</summary>
    public static List<T[]> Split<T>(IReadOnlyList<T> moves, int size)
    {
        if (size < 1) throw new ArgumentOutOfRangeException(nameof(size));
        return moves.Chunk(size).ToList();
    }

    /// <summary>The moves whose folder is not among the child folders the server listed for its
    /// destination (destination id -> ids listed there). A destination with no listing counts as
    /// empty, so its moves are returned. Order is kept.</summary>
    public static List<T> NotThere<T>(IEnumerable<T> moves, Func<T, string> folder, Func<T, string> destination,
        IReadOnlyDictionary<string, HashSet<string>> listed)
    {
        var missing = new List<T>();
        foreach (var m in moves)
            if (!listed.TryGetValue(destination(m), out var there) || !there.Contains(folder(m)))
                missing.Add(m);
        return missing;
    }
}
