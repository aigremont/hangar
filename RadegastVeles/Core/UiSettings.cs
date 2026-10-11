/*
 * Radegast Metaverse Client
 * Copyright (c) 2026, Sjofn LLC
 * All rights reserved.
 *
 * Radegast is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Lesser General Public License as published
 * by the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU Lesser General Public License
 * along with this program. If not, see <https://www.gnu.org/licenses/>.
 */

using System;
using System.IO;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace Radegast.Veles.Core;

/// <summary>
/// Tiny application-wide (not per-account) UI settings store, readable before any agent
/// instance exists. Backed by %APPDATA%\RadegastVeles\ui-settings.json.
/// Keys: "ClassicTheme" (bool, default true).
/// </summary>
public static class UiSettings
{
    private const string ClassicThemeKey = "ClassicTheme";

    private static readonly string FilePath = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
        "RadegastVeles", "ui-settings.json");

    /// <summary>Compact "classic EVE" theme; applied at startup, so changes need a restart.</summary>
    public static bool ClassicTheme
    {
        get
        {
            try
            {
                if (File.Exists(FilePath) &&
                    JsonNode.Parse(File.ReadAllText(FilePath)) is JsonObject o &&
                    o[ClassicThemeKey] is JsonValue v && v.TryGetValue(out bool b))
                    return b;
            }
            catch { }
            return true;
        }
        set
        {
            try
            {
                JsonObject o = new();
                try
                {
                    if (File.Exists(FilePath) && JsonNode.Parse(File.ReadAllText(FilePath)) is JsonObject existing)
                        o = existing;
                }
                catch { }
                o[ClassicThemeKey] = value;
                Directory.CreateDirectory(Path.GetDirectoryName(FilePath)!);
                File.WriteAllText(FilePath, o.ToJsonString(new JsonSerializerOptions { WriteIndented = true }));
            }
            catch { }
        }
    }
}
