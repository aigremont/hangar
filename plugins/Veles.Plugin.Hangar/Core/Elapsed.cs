using System;

namespace Veles.Plugin.Hangar.Core;

/// <summary>Formats a duration for progress messages.</summary>
public static class Elapsed
{
    /// <summary>"45s", "1m 23s" or "1h 02m 03s"; negative durations count as zero.</summary>
    public static string Format(TimeSpan t)
    {
        if (t < TimeSpan.Zero) t = TimeSpan.Zero;
        long total = (long)t.TotalSeconds;
        long h = total / 3600, m = total % 3600 / 60, s = total % 60;
        if (h > 0) return $"{h}h {m:00}m {s:00}s";
        if (m > 0) return $"{m}m {s:00}s";
        return $"{s}s";
    }
}
