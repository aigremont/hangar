using System;
using System.Collections.Generic;

namespace Veles.Plugin.Hangar.Core;

/// <summary>One buffered chat line with its sequence number (the first line ever added is 1).</summary>
public readonly record struct LogLine(long Seq, string Text);

/// <summary>A fixed-size ring buffer of the lines Hangar said, each with a running sequence number, so a
/// caller can ask for "everything after n". Thread-safe.</summary>
public sealed class LineLog
{
    readonly object _lock = new();
    readonly string[] _buffer;
    long _last;   // sequence number of the newest line; 0 = none yet

    public LineLog(int capacity = 2000)
    {
        if (capacity <= 0) throw new ArgumentOutOfRangeException(nameof(capacity));
        _buffer = new string[capacity];
    }

    public int Capacity => _buffer.Length;

    /// <summary>Sequence number of the newest line (0 if nothing was added).</summary>
    public long LastSeq { get { lock (_lock) return _last; } }

    /// <summary>Adds a line; text with line breaks becomes one entry per line. Returns the last sequence number.</summary>
    public long Add(string text)
    {
        lock (_lock)
        {
            foreach (var line in (text ?? "").Split('\n'))
                _buffer[(int)(++_last % _buffer.Length)] = line.TrimEnd('\r');
            return _last;
        }
    }

    /// <summary>The lines with a sequence number greater than <paramref name="since"/>, oldest first, and the
    /// sequence number to pass next time. <c>Truncated</c> is true if lines after <paramref name="since"/> were
    /// already overwritten.</summary>
    public (List<LogLine> Lines, long Next, bool Truncated) Since(long since)
    {
        lock (_lock)
        {
            if (since < 0) since = 0;
            if (since > _last) since = _last;   // a caller ahead of us (e.g. after a plugin reload) starts over from now
            long oldest = Math.Max(1, _last - _buffer.Length + 1);
            bool truncated = since + 1 < oldest;
            var lines = new List<LogLine>();
            for (long s = Math.Max(since + 1, oldest); s <= _last; s++)
                lines.Add(new LogLine(s, _buffer[(int)(s % _buffer.Length)]));
            return (lines, _last, truncated);
        }
    }
}
