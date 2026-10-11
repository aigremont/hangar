using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using NUnit.Framework;
using Veles.Plugin.Hangar.Core;

namespace Veles.Plugin.Hangar.Tests;

sealed class FakeExportSource : IExportSource
{
    readonly Dictionary<string, ExportFolder> _folders = new();
    readonly List<ExportItem> _items = new();
    public string RootId => "root";

    public FakeExportSource() => _folders["root"] = new ExportFolder("root", "0", "My Inventory", "Root");

    public FakeExportSource Folder(string id, string parent, string name, string type = "None")
    {
        _folders[id] = new ExportFolder(id, parent, name, type);
        return this;
    }

    public FakeExportSource Item(string id, string parent, string name, string creator = "c1", bool link = false)
    {
        _items.Add(new ExportItem(id, parent, name, link ? "Link" : "Object", "Object", null, creator, "me",
            0, 581632, 532480, link, link ? "target" : null));
        return this;
    }

    public ExportFolder? Folder(string id) => _folders.GetValueOrDefault(id);

    public (IReadOnlyList<ExportFolder>, IReadOnlyList<ExportItem>) Contents(string id) =>
        (_folders.Values.Where(f => f.Parent == id).ToList(), _items.Where(i => i.Parent == id).ToList());
}

public class ExportTests
{
    static FakeExportSource Sample() => new FakeExportSource()
        .Folder("trash", "root", "Trash", "Trash")
        .Folder("brand", "root", "ALDER")
        .Folder("phantom", "root", "Not on server")
        .Folder("deep", "brand", "Sub")
        .Item("i1", "brand", "Dress", "c1")
        .Item("i2", "deep", "Hat", "c2")
        .Item("i3", "phantom", "Ghost", "c3")
        .Item("i4", "root", "Loose", "00000000-0000-0000-0000-000000000000")
        .Item("l1", "brand", "Dress link", "c1", link: true);

    static readonly HashSet<string> Confirmed = new() { "root", "trash", "brand", "deep" };

    [Test]
    public void BuildKeepsOnlyConfirmedFoldersAndTheirItems()
    {
        var c = InventoryExporter.Build(Sample(), Confirmed);
        Assert.That(c.Folders.Select(f => f.Id), Is.EquivalentTo(new[] { "root", "trash", "brand", "deep" }));
        Assert.That(c.Items.Select(i => i.Id), Is.EquivalentTo(new[] { "i1", "i2", "i4", "l1" }));
        Assert.That(c.CreatorIds, Is.EqualTo(new[] { "c1", "c2" }), "distinct, no null id, none from unconfirmed folders");
    }

    [Test]
    public void WrittenJsonHasTheDocumentedShape()
    {
        var c = InventoryExporter.Build(Sample(), Confirmed);
        using var ms = new MemoryStream();
        InventoryExporter.Write(ms, new DateTimeOffset(2026, 10, 2, 12, 0, 0, TimeSpan.Zero), "me", "Test Résident",
            "root", c, new Dictionary<string, string> { ["c1"] = "Brand One (Brandy)" });
        using var doc = JsonDocument.Parse(ms.ToArray());
        var r = doc.RootElement;
        Assert.That(r.GetProperty("format").GetString(), Is.EqualTo("hangar-export"));
        Assert.That(r.GetProperty("version").GetInt32(), Is.EqualTo(1));
        Assert.That(r.GetProperty("agent_name").GetString(), Is.EqualTo("Test Résident"));
        Assert.That(r.GetProperty("folders").GetArrayLength(), Is.EqualTo(4));
        Assert.That(r.GetProperty("folders")[1].GetProperty("type").GetString(), Is.EqualTo("Trash"));
        var items = r.GetProperty("items");
        Assert.That(items.GetArrayLength(), Is.EqualTo(4));
        var link = items.EnumerateArray().Single(i => i.GetProperty("id").GetString() == "l1");
        Assert.That(link.GetProperty("is_link").GetBoolean(), Is.True);
        Assert.That(link.GetProperty("link_target").GetString(), Is.EqualTo("target"));
        var plain = items.EnumerateArray().Single(i => i.GetProperty("id").GetString() == "i1");
        Assert.That(plain.TryGetProperty("link_target", out _), Is.False);
        Assert.That(plain.TryGetProperty("wearable_type", out _), Is.False);
        Assert.That(plain.GetProperty("perms").GetProperty("next_owner").GetInt32(), Is.EqualTo(532480));
        Assert.That(r.GetProperty("creators").EnumerateObject().Select(p => p.Name), Is.EqualTo(new[] { "c1" }),
            "unresolved creators are left out");
        Assert.That(System.Text.Encoding.UTF8.GetString(ms.ToArray()), Does.Contain("Résident"), "UTF-8, not unicode escapes");
    }

    [Test]
    public void FileNameUsesUnderscoresForSpaces()
    {
        Assert.That(InventoryExporter.FileName("Test Resident", new DateTime(2026, 10, 2, 9, 5, 7)),
            Is.EqualTo("hangar-export-Test_Resident-20261002-090507.json"));
    }

    [TestCase("Brand", "Resident", "brand.resident", "Brandy", false, "Brand Resident (Brandy)")]
    [TestCase("Brand", "Resident", "brand.resident", "brand resident", false, "Brand Resident")]
    [TestCase("Brand", "Resident", "brand.resident", "Brand", true, "Brand Resident")]
    [TestCase("", "", "brand.resident", "", false, "brand.resident")]
    public void CreatorNameFormat(string first, string last, string user, string display, bool isDefault, string want) =>
        Assert.That(InventoryExporter.FormatCreator(first, last, user, display, isDefault), Is.EqualTo(want));

    [Test]
    public void BatchesSplitEvenly()
    {
        var sizes = InventoryExporter.Batches(Enumerable.Range(0, 120), 50).Select(b => b.Count).ToList();
        Assert.That(sizes, Is.EqualTo(new[] { 50, 50, 20 }));
    }
}
