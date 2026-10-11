// Hangar: applies an inventory plan written by tools/inventory/inventory_triage.py --plan.
// Core logic: no Veles, Avalonia or LibreMetaverse types, so it is unit-tested on its own.

using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;

namespace Veles.Plugin.Hangar.Core;

/// <summary>One row of plan.csv: move one product folder to a destination path.</summary>
public sealed record PlanRow(
    string ProductFolder,
    int Items,
    string From,
    string To,
    string Because,
    string FolderId,
    string FromId,
    string RenameTo = "",
    bool AllowProtected = false);

public static class PlanReader
{
    public const string PathSeparator = " > ";

    static readonly string[] Required = { "product_folder", "to", "folder_id", "from_id" };

    /// <summary>Reads plan.csv (UTF-8, optional BOM, RFC 4180 quoting).</summary>
    public static List<PlanRow> Read(TextReader reader) => FromRecords(Csv.Parse(reader));

    /// <summary>The header names of a plan: trimmed, without a byte order mark.</summary>
    public static List<string> HeaderOf(List<string> record) =>
        record.Select(h => h.Trim().TrimStart('﻿')).ToList();

    /// <summary>Builds the rows from parsed CSV records, the first being the header.</summary>
    public static List<PlanRow> FromRecords(List<List<string>> records)
    {
        if (records.Count == 0)
            throw new InvalidDataException("the plan is empty");
        var header = HeaderOf(records[0]);
        foreach (var name in Required)
        {
            if (!header.Contains(name))
                throw new InvalidDataException(
                    $"the plan has no '{name}' column; write it again with a current inventory_triage.py --plan");
        }

        string Field(List<string> r, string name)
        {
            int i = header.IndexOf(name);
            return i >= 0 && i < r.Count ? r[i] : "";
        }

        var rows = new List<PlanRow>();
        for (int n = 1; n < records.Count; n++)
        {
            var r = records[n];
            if (r.Count == 1 && r[0].Length == 0) continue;     // blank line
            int.TryParse(Field(r, "items"), out int items);
            rows.Add(new PlanRow(Field(r, "product_folder"), items, Field(r, "from"),
                Field(r, "to"), Field(r, "because"), Field(r, "folder_id").Trim(),
                Field(r, "from_id").Trim(), Field(r, "rename_to").Trim(),
                Field(r, "allow_protected").Trim().Equals("yes", StringComparison.OrdinalIgnoreCase)));
        }
        return rows;
    }

    public static string[] SplitPath(string path) =>
        path.Split(new[] { ">" }, StringSplitOptions.None)
            .Select(p => p.Trim())
            .Where(p => p.Length > 0)
            .ToArray();
}

/// <summary>Minimal RFC 4180 CSV reader and writer.</summary>
public static class Csv
{
    public static List<List<string>> Parse(TextReader reader)
    {
        var rows = new List<List<string>>();
        var row = new List<string>();
        var field = new StringBuilder();
        bool quoted = false, any = false;
        int c;
        while ((c = reader.Read()) != -1)
        {
            any = true;
            char ch = (char)c;
            if (quoted)
            {
                if (ch == '"')
                {
                    if (reader.Peek() == '"') { field.Append('"'); reader.Read(); }
                    else quoted = false;
                }
                else field.Append(ch);
                continue;
            }
            switch (ch)
            {
                case '"': quoted = true; break;
                case ',': row.Add(field.ToString()); field.Clear(); break;
                case '\r': break;
                case '\n':
                    row.Add(field.ToString()); field.Clear();
                    rows.Add(row); row = new List<string>();
                    any = false;
                    break;
                default: field.Append(ch); break;
            }
        }
        if (any)
        {
            row.Add(field.ToString());
            rows.Add(row);
        }
        return rows;
    }

    public static string Escape(string s) =>
        s.IndexOfAny(new[] { ',', '"', '\n', '\r' }) >= 0 ? "\"" + s.Replace("\"", "\"\"") + "\"" : s;

    public static string Line(params object?[] fields) =>
        string.Join(",", fields.Select(f => Escape(f?.ToString() ?? "")));
}
