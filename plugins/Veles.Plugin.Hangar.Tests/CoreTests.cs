using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using NUnit.Framework;
using Veles.Plugin.Hangar.Core;

namespace Veles.Plugin.Hangar.Tests;

/// <summary>An in-memory inventory for the checks.</summary>
sealed class FakeTree : IFolderTree
{
    readonly Dictionary<string, FolderInfo> _folders = new();
    public string RootId => "root";

    public FakeTree() => _folders["root"] = new FolderInfo("root", "My Inventory", "", "Root");

    public FakeTree Add(string id, string name, string parent, string kind = "None")
    {
        _folders[id] = new FolderInfo(id, name, parent, kind);
        return this;
    }

    public void Move(string id, string parent) => _folders[id] = _folders[id] with { ParentId = parent };

    public FolderInfo? Get(string id) => _folders.GetValueOrDefault(id);

    public IEnumerable<FolderInfo> ChildFolders(string id) => _folders.Values.Where(f => f.ParentId == id);
}

public class CoreTests
{
    static FakeTree Sample() => new FakeTree()
        .Add("clothing", "Clothing", "root", "Clothing")
        .Add("clothes", "Clothes", "clothing")
        .Add("alder", "ALDER", "clothes")
        .Add("objects", "Objects", "root", "Object")
        .Add("inbox", "Received Items", "root", "Inbox")
        .Add("p1", "ALDER - Hattie Sweater", "root")
        .Add("p2", "TRV puffy straitjacket", "inbox")
        .Add("p3", "Sable Rocky Shore", "root")
        .Add("rlv", "#RLV", "root")
        .Add("p4", "[BB] Bikini", "rlv")
        .Add("trash", "Trash", "root", "Trash")
        .Add("p5", "Old thing", "trash")
        .Add("p6", "Parent product", "root")
        .Add("p6a", "HUD", "p6");

    static PlanRow Row(string id, string from, string to, string name = "x") =>
        new(name, 3, "", to, "test", id, from);

    [Test]
    public void ReadsPlanCsvWithBomQuotesAndIds()
    {
        var csv = "﻿product_folder,items,from,to,because,creates_folder,folder_id,from_id\r\n" +
                  "\"Moon, \"\"Elixir\"\" Set\",4,(root),Clothing > Clothes > Moon,your decision,yes,a1,r1\r\n" +
                  "Plain,2,Received Items,Objects > X,existing,,a2,r2\r\n";
        var rows = PlanReader.Read(new StringReader(csv));
        Assert.That(rows, Has.Count.EqualTo(2));
        Assert.That(rows[0].ProductFolder, Is.EqualTo("Moon, \"Elixir\" Set"));
        Assert.That(rows[0].Items, Is.EqualTo(4));
        Assert.That(rows[0].FolderId, Is.EqualTo("a1"));
        Assert.That(rows[1].FromId, Is.EqualTo("r2"));
    }

    [Test]
    public void RejectsAPlanWithoutIds()
    {
        var csv = "product_folder,items,from,to,because,creates_folder\nA,1,x,y,z,\n";
        Assert.Throws<InvalidDataException>(() => PlanReader.Read(new StringReader(csv)));
    }

    [Test]
    public void MovesIntoAnExistingFolder()
    {
        var plan = PlanChecker.Check(new[] { Row("p1", "root", "Clothing > Clothes > ALDER") }, Sample());
        var r = plan.Rows.Single();
        Assert.That(r.Outcome, Is.EqualTo(Outcome.Move));
        Assert.That(r.DestinationId, Is.EqualTo("alder"));
        Assert.That(plan.FoldersToCreate, Is.Empty);
    }

    [Test]
    public void CreatesMissingFoldersParentsFirstAndOnce()
    {
        var plan = PlanChecker.Check(new[]
        {
            Row("p3", "root", "Objects > Landscaping > Sable"),
            Row("p2", "inbox", "Objects > Landscaping > Grove"),
        }, Sample());
        Assert.That(plan.Moves.Count(), Is.EqualTo(2));
        Assert.That(plan.FoldersToCreate, Is.EqualTo(new[]
        {
            "Objects > Landscaping",
            "Objects > Landscaping > Grove",
            "Objects > Landscaping > Sable",
        }));
    }

    [Test]
    public void SkipsWhatChangedSinceThePlan()
    {
        var tree = Sample();
        tree.Move("p3", "objects");
        var plan = PlanChecker.Check(new[]
        {
            Row("gone", "root", "Objects > A"),
            Row("p3", "root", "Objects > B"),
        }, tree);
        Assert.That(plan.Rows[0].Reason, Is.EqualTo("no longer in the inventory"));
        Assert.That(plan.Rows[1].Outcome, Is.EqualTo(Outcome.Skip));
        Assert.That(plan.Rows[1].Reason, Does.StartWith("moved since the plan was made (now in Objects)"));
    }

    [Test]
    public void RecognisesAFolderAlreadyInPlace()
    {
        var tree = Sample();
        tree.Move("p1", "alder");
        var plan = PlanChecker.Check(new[] { Row("p1", "root", "Clothing > Clothes > ALDER") }, tree);
        Assert.That(plan.Rows.Single().Outcome, Is.EqualTo(Outcome.AlreadyThere));
    }

    [Test]
    public void NeverTouchesProtectedAreas()
    {
        var plan = PlanChecker.Check(new[]
        {
            Row("p4", "rlv", "Clothing > Clothes > BB"),
            Row("p5", "trash", "Objects > Old"),
            Row("p1", "root", "#RLV > Tops"),
            Row("inbox", "root", "Objects > Inbox"),
        }, Sample());
        Assert.That(plan.Rows.Select(r => r.Outcome), Is.All.EqualTo(Outcome.Skip));
        Assert.That(plan.Rows[0].Reason, Does.Contain("#RLV"));
        Assert.That(plan.Rows[1].Reason, Does.Contain("Trash"));
        Assert.That(plan.Rows[2].Reason, Does.StartWith("destination"));
        Assert.That(plan.Rows[3].Reason, Does.Contain("system folder"));
        Assert.That(plan.FoldersToCreate, Is.Empty);
    }

    [Test]
    public void NeverMovesAFolderIntoItself()
    {
        var plan = PlanChecker.Check(new[] { Row("p6", "root", "Parent product > HUD") }, Sample());
        Assert.That(plan.Rows.Single().Reason, Is.EqualTo("destination is inside the folder itself"));
    }

    [Test]
    public void SkipsADuplicateRow()
    {
        var plan = PlanChecker.Check(new[]
        {
            Row("p1", "root", "Clothing > Clothes > ALDER"),
            Row("p1", "root", "Objects"),
        }, Sample());
        Assert.That(plan.Rows[1].Reason, Is.EqualTo("the plan lists this folder twice"));
    }

    [Test]
    public void MatchesNamesIgnoringCaseOnlyWhenUnambiguous()
    {
        var tree = Sample().Add("o2", "alder", "clothes");
        // "ALDER" matches exactly even though "alder" also exists.
        var plan = PlanChecker.Check(new[] { Row("p1", "root", "Clothing > Clothes > ALDER") }, tree);
        Assert.That(plan.Rows.Single().DestinationId, Is.EqualTo("alder"));
        // "Alder" matches both loosely: treated as missing, so it would be created.
        var plan2 = PlanChecker.Check(new[] { Row("p3", "root", "Clothing > Clothes > Alder") }, tree);
        Assert.That(plan2.FoldersToCreate, Is.EqualTo(new[] { "Clothing > Clothes > Alder" }));
    }

    [Test]
    public void JournalRoundTripsAndUndoesOnlyWhatIsStillInPlace()
    {
        var path = Path.GetTempFileName();
        File.Delete(path);
        try
        {
            using (var j = new Journal(path))
            {
                j.Write(new JournalEntry(DateTime.UtcNow, JournalKind.Create, "new1", "", "objects", "Landscaping"));
                j.Write(new JournalEntry(DateTime.UtcNow, JournalKind.Move, "p1", "root", "alder", "Hattie\tSweater"));
                j.Write(new JournalEntry(DateTime.UtcNow, JournalKind.Move, "p3", "root", "objects", "Sable"));
                j.Write(new JournalEntry(DateTime.UtcNow, JournalKind.Move, "gone", "root", "objects", "Gone"));
            }
            List<JournalEntry> entries;
            using (var r = new StreamReader(path)) entries = Journal.Read(r);
            Assert.That(entries, Has.Count.EqualTo(4));
            Assert.That(entries[1].Name, Is.EqualTo("Hattie Sweater"));

            var tree = Sample();
            tree.Move("p1", "alder");         // where Hangar put it
            tree.Move("p3", "clothes");       // moved again by hand since
            var (moves, skipped, created) = Journal.PlanUndo(entries, tree);
            Assert.That(moves.Select(m => (m.FolderId, m.NewParentId)), Is.EqualTo(new[] { ("p1", "root") }));
            Assert.That(skipped, Has.Count.EqualTo(2));
            Assert.That(created.Single().Name, Is.EqualTo("Landscaping"));
        }
        finally { File.Delete(path); }
    }

    static PlanRow Rlv(string id, string from, string to, string rename = "", bool allow = true) =>
        new("x", 1, "", to, "your decision", id, from, rename, allow);

    [Test]
    public void ReadsRenameAndDeliberateProtectedColumns()
    {
        var csv = "product_folder,items,from,to,because,creates_folder,folder_id,from_id,rename_to,allow_protected\n" +
                  "Pierced Pasties Package PBR [ADD],1,(root),#RLV > Accessories > Piercings,your decision,yes,a1,r1,Pierced Pasties,yes\n" +
                  "Plain,2,x,Objects > X,existing,,a2,r2,,\n";
        var rows = PlanReader.Read(new StringReader(csv));
        Assert.That((rows[0].RenameTo, rows[0].AllowProtected), Is.EqualTo(("Pierced Pasties", true)));
        Assert.That((rows[1].RenameTo, rows[1].AllowProtected), Is.EqualTo(("", false)));
    }

    static FakeTree RlvSample() => Sample()
        .Add("rarea", "Tops", "rlv")
        .Add("old", "[BB] Old Layout", "rarea")
        .Add("vend", "~vendor", "rlv")
        .Add("vprod", "Vendor product", "vend")
        .Add("dvend", ".~vendor", "rlv")
        .Add("wi", "Wardrobe Import", "root")
        .Add("wprod", "Imported dress", "wi")
        .Add("tprod", "Trashed product", "trash");

    static PlanRow Flagged(string id, string from, string to) =>
        Row(id, from, to) with { AllowProtected = true };

    [Test]
    public void AllowProtectedLetsASourceInsideRlvBeRefiled()
    {
        var with = PlanChecker.Check(new[] { Flagged("old", "rarea", "#RLV > Tops > Bikinis") }, RlvSample());
        Assert.That(with.Rows.Single().Outcome, Is.EqualTo(Outcome.Move));
        var without = PlanChecker.Check(new[] { Row("old", "rarea", "#RLV > Tops > Bikinis") }, RlvSample());
        Assert.That(without.Rows.Single().Outcome, Is.EqualTo(Outcome.Skip));
        Assert.That(without.Rows.Single().Reason, Does.Contain("#RLV"));
    }

    [Test]
    public void AllowProtectedStillRefusesVendorManagedSources()
    {
        var plan = PlanChecker.Check(new[]
        {
            Flagged("vend", "rlv", "#RLV > Tops > X"),
            Flagged("vprod", "vend", "#RLV > Tops > Y"),
            Flagged("dvend", "rlv", "#RLV > Tops > Z"),
        }, RlvSample());
        Assert.That(plan.Rows.Select(r => r.Outcome), Is.All.EqualTo(Outcome.Skip));
        Assert.That(plan.Rows.Select(r => r.Reason), Is.All.Contain("vendor-managed"));
    }

    [Test]
    public void AllowProtectedLetsWardrobeImportBeEmptiedButNotTheTrash()
    {
        var plan = PlanChecker.Check(new[]
        {
            Flagged("wprod", "wi", "#RLV > Tops > Dress"),
            Flagged("tprod", "trash", "#RLV > Tops > Old"),
        }, RlvSample());
        Assert.That(plan.Rows[0].Outcome, Is.EqualTo(Outcome.Move));
        Assert.That(plan.Rows[1].Outcome, Is.EqualTo(Outcome.Skip));
        Assert.That(plan.Rows[1].Reason, Does.Contain("Trash"));
    }

    [Test]
    public void FilesIntoRlvOnlyWhenTheUserChoseIt()
    {
        var chosen = PlanChecker.Check(new[] { Rlv("p1", "root", "#RLV > Accessories > Piercings", "Pierced Pasties") }, Sample());
        var r = chosen.Rows.Single();
        Assert.That((r.Outcome, r.Rename), Is.EqualTo((Outcome.Move, "Pierced Pasties")));
        Assert.That(chosen.FoldersToCreate, Is.EqualTo(new[] { "#RLV > Accessories", "#RLV > Accessories > Piercings" }));

        var notChosen = PlanChecker.Check(new[] { Rlv("p1", "root", "#RLV > Accessories", allow: false) }, Sample());
        Assert.That(notChosen.Rows.Single().Reason, Does.StartWith("destination inside a protected folder"));
    }

    [Test]
    public void NeverFilesIntoSystemAreasEvenWhenChosen()
    {
        var plan = PlanChecker.Check(new[] { Rlv("p1", "root", "Trash > Keep") }, Sample());
        Assert.That(plan.Rows.Single().Reason, Does.Contain("Trash"));
    }

    [Test]
    public void NeverCreatesAProtectedFolderUnasked()
    {
        var plan = PlanChecker.Check(new[] { Row("p1", "root", "Objects > RLV Spare") }, Sample());
        Assert.That(plan.Rows.Single().Reason, Is.EqualTo("destination would create a protected folder"));
    }

    [Test]
    public void RenamesInPlaceAndRefusesANameClash()
    {
        var tree = Sample().Add("dup", "Hattie", "alder");
        tree.Move("p1", "alder");
        var onlyRename = PlanChecker.Check(new[] { Rlv("p1", "root", "Clothing > Clothes > ALDER", "Hattie Sweater", false) }, tree);
        Assert.That((onlyRename.Rows.Single().Outcome, onlyRename.Rows.Single().Reason),
            Is.EqualTo((Outcome.Move, "in place; rename only")));
        var clash = PlanChecker.Check(new[] { Rlv("p1", "root", "Clothing > Clothes > ALDER", "Hattie", false) }, tree);
        Assert.That(clash.Rows.Single().Reason, Is.EqualTo("a folder named Hattie is already there"));
    }

    [Test]
    public void UndoRestoresANameOnlyIfItIsStillHangars()
    {
        var path = Path.GetTempFileName();
        File.Delete(path);
        try
        {
            using (var j = new Journal(path))
            {
                j.Write(new JournalEntry(DateTime.UtcNow, JournalKind.Move, "p1", "root", "alder", "Hattie"));
                j.Write(new JournalEntry(DateTime.UtcNow, JournalKind.Rename, "p1", "alder", "alder", "Hattie Sweater", "ALDER - Hattie Sweater"));
                j.Write(new JournalEntry(DateTime.UtcNow, JournalKind.Rename, "p3", "root", "root", "Sable", "Sable Rocky Shore"));
            }
            List<JournalEntry> entries;
            using (var r = new StreamReader(path)) entries = Journal.Read(r);
            Assert.That(entries[1].OldName, Is.EqualTo("ALDER - Hattie Sweater"));

            var tree = new FakeTree()
                .Add("alder", "ALDER", "root")
                .Add("p1", "Hattie Sweater", "alder")     // as Hangar left it
                .Add("p3", "Renamed by hand", "root");    // changed since
            var (moves, renames, skipped, _) = Journal.PlanUndoAll(entries, tree);
            Assert.That(renames.Select(r => (r.FolderId, r.Name)), Is.EqualTo(new[] { ("p1", "ALDER - Hattie Sweater") }));
            Assert.That(moves.Single().NewParentId, Is.EqualTo("root"));
        }
        finally { File.Delete(path); }
    }

    [Test]
    public void PreviewListsEveryRowAndEveryNewFolder()
    {
        var plan = PlanChecker.Check(new[]
        {
            Row("p1", "root", "Clothing > Clothes > ALDER", "Hattie, Sweater"),
            Row("p3", "root", "Objects > Landscaping > Sable"),
        }, Sample());
        var w = new StringWriter();
        plan.WritePreview(w);
        var lines = Csv.Parse(new StringReader(w.ToString()));
        Assert.That(lines[0][0], Is.EqualTo("outcome"));
        Assert.That(lines[1][1], Is.EqualTo("Hattie, Sweater"));
        Assert.That(lines.Count(l => l[0] == "CreateFolder"), Is.EqualTo(2));
        Assert.That(plan.Summary(), Does.StartWith("2 folder moves (6 items), 2 new folders"));
    }
}

[TestFixture]
public class ApplyReportTests
{
    [Test]
    public void Failed_picks_only_false_results_in_order()
    {
        var failed = ApplyReport.Failed(new[] { "a", "b", "c", "d" }, new[] { true, false, true, false });
        Assert.That(failed, Is.EqualTo(new[] { "b", "d" }));
    }

    [Test]
    public void Failed_treats_missing_results_as_failures()
    {
        Assert.That(ApplyReport.Failed(new[] { "a", "b" }, new[] { true }), Is.EqualTo(new[] { "b" }));
    }

    [Test]
    public void FailureLine_names_the_first_ten_and_counts_the_rest()
    {
        var names = Enumerable.Range(1, 13).Select(i => "f" + i).ToList();
        var line = ApplyReport.FailureLine("move", names);
        Assert.That(line, Does.StartWith("move failed for 13: f1, f2"));
        Assert.That(line, Does.Contain("f10"));
        Assert.That(line, Does.Not.Contain("f11"));
        Assert.That(line, Does.EndWith("(and 3 more)"));
        Assert.That(ApplyReport.FailureLine("move", new string[0]), Is.EqualTo(""));
    }

    [Test]
    public void Summary_reports_all_four_counts()
    {
        Assert.That(ApplyReport.Summary(5, 1, 2, 3), Is.EqualTo("apply finished: moved 5, failed 1, renamed 2, created 3"));
    }
}

public class PhantomTests
{
    static PlanRow Row(string id, string from, string to) =>
        new("x", 3, "", to, "test", id, from);

    static FakeTree Sample() => new FakeTree()
        .Add("objects", "Objects", "root", "Object")
        .Add("inbox", "Received Items", "root", "Inbox");

    // The incident: "Objects > To Unpack > BrandX" exists locally (client-made ids) but the server never had it.
    static FakeTree Phantom() => Sample()
        .Add("tu", "To Unpack", "objects")
        .Add("cbb", "BrandX", "tu")
        .Add("src", "Some Product", "root");

    [Test]
    public void ConfirmedTree_hides_folders_the_server_did_not_return()
    {
        var confirmed = new HashSet<string> { "objects", "src" };
        var tree = new ConfirmedTree(Phantom(), confirmed);
        Assert.That(tree.Get("tu"), Is.Null);
        Assert.That(tree.Get("cbb"), Is.Null);
        Assert.That(tree.Get("objects"), Is.Not.Null);
        Assert.That(tree.Get("root"), Is.Not.Null);
        Assert.That(tree.ChildFolders("objects"), Is.Empty);
        Assert.That(tree.ChildFolders("tu"), Is.Empty);
        Assert.That(tree.ChildFolders("root").Select(f => f.Id), Is.EquivalentTo(new[] { "objects", "src" }));
    }

    [Test]
    public void Checker_plans_creates_for_unconfirmed_destination()
    {
        var plan = PlanChecker.Check(new[] { Row("src", "root", "Objects > To Unpack > BrandX") },
            new ConfirmedTree(Phantom(), new HashSet<string> { "objects", "src" }));
        Assert.That(plan.FoldersToCreate, Is.EqualTo(new[] { "Objects > To Unpack", "Objects > To Unpack > BrandX" }));
        Assert.That(plan.Moves.Single().DestinationId, Is.Null);
    }

    [Test]
    public void Checker_uses_confirmed_destination()
    {
        var plan = PlanChecker.Check(new[] { Row("src", "root", "Objects > To Unpack > BrandX") },
            new ConfirmedTree(Phantom(), new HashSet<string> { "objects", "src", "tu", "cbb" }));
        Assert.That(plan.FoldersToCreate, Is.Empty);
        Assert.That(plan.Moves.Single().DestinationId, Is.EqualTo("cbb"));
    }

    [Test]
    public void Phantoms_are_local_folders_outside_the_confirmed_set_and_system_ones_are_not_prunable()
    {
        var tree = Phantom().Add("sys", "Odd", "root", "Favorites");
        var local = tree.ChildFolders("objects").Concat(tree.ChildFolders("tu")).Concat(new[] { tree.Get("sys")!, tree.Get("src")! });
        var found = Phantoms.Find(local, new HashSet<string> { "objects", "src" });
        Assert.That(found.Select(f => f.Id), Is.EquivalentTo(new[] { "tu", "cbb", "sys" }));
        Assert.That(Phantoms.Prunable(tree.Get("tu")!), Is.True);
        Assert.That(Phantoms.Prunable(tree.Get("sys")!), Is.False);
    }
}

[TestFixture]
public class MoveBatchesTests
{
    [Test]
    public void SplitsIntoChunksOfTwentyKeepingOrder()
    {
        var moves = Enumerable.Range(1, 45).ToList();
        var parts = MoveBatches.Split(moves, 20);
        Assert.That(parts.Select(p => p.Length), Is.EqualTo(new[] { 20, 20, 5 }));
        Assert.That(parts.SelectMany(p => p), Is.EqualTo(moves));
    }

    [Test]
    public void SplitOfNothingIsNoMessages() =>
        Assert.That(MoveBatches.Split(new List<int>(), 20), Is.Empty);

    [Test]
    public void ExactMultipleHasNoEmptyTail() =>
        Assert.That(MoveBatches.Split(Enumerable.Range(0, 40).ToList(), 20).Count, Is.EqualTo(2));

    [Test]
    public void NotThereReturnsOnlyFoldersAbsentFromTheirDestination()
    {
        var moves = new[] { ("f1", "d1"), ("f2", "d1"), ("f3", "d2"), ("f4", "d3") };
        var listed = new Dictionary<string, HashSet<string>>
        {
            ["d1"] = new() { "f1", "other" },
            ["d2"] = new() { "f2" },          // f2 listed in the wrong place does not count for f3
        };
        var missing = MoveBatches.NotThere(moves, m => m.Item1, m => m.Item2, listed);
        Assert.That(missing.Select(m => m.Item1), Is.EqualTo(new[] { "f2", "f3", "f4" }));
    }

    [Test]
    public void NotThereIsEmptyWhenEverythingLanded()
    {
        var moves = new[] { ("f1", "d1"), ("f2", "d1") };
        var listed = new Dictionary<string, HashSet<string>> { ["d1"] = new() { "f1", "f2" } };
        Assert.That(MoveBatches.NotThere(moves, m => m.Item1, m => m.Item2, listed), Is.Empty);
    }
}
[TestFixture]
public class ElapsedTests
{
    [TestCase(0, "0s")]
    [TestCase(45, "45s")]
    [TestCase(83, "1m 23s")]
    [TestCase(60, "1m 00s")]
    [TestCase(3723, "1h 02m 03s")]
    [TestCase(-5, "0s")]
    public void Formats(int seconds, string expected) =>
        Assert.That(Elapsed.Format(TimeSpan.FromSeconds(seconds)), Is.EqualTo(expected));
}
