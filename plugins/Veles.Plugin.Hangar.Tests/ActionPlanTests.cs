using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using NUnit.Framework;
using Veles.Plugin.Hangar.Core;

namespace Veles.Plugin.Hangar.Tests;

public class ActionPlanTests
{
    static FakeTree Sample() => new FakeTree()
        .Add("objects", "Objects", "root", "Object")
        .Add("notecards", "Notecards", "root", "Notecard")
        .Add("trash", "Trash", "root", "Trash")
        .Add("inbox", "Received Items", "root", "Inbox")
        .Add("rlv", "#RLV", "root")
        .Add("tops", "Tops", "rlv")
        .Add("prod", "Pretty Dress", "root")
        .Add("hud", "HUD", "prod")
        .Add("old", "Old Box", "trash")
        .Add("loose", "Loose Things", "root");

    static ItemIndex Items() => new(new[]
    {
        new ItemInfo("note1", "Read me", "prod", "Notecard"),
        new ItemInfo("lm1", "Vendor LM", "prod", "Landmark"),
        new ItemInfo("rlvitem", "Hidden shirt", "tops", "Clothing"),
        new ItemInfo("trashed", "Binned", "trash", "Texture"),
        new ItemInfo("deep", "Deep binned", "old", "Texture"),
        new ItemInfo("fresh", "From a gift", "inbox", "Object"),
        new ItemInfo("docnote", "Already filed", "notecards", "Notecard"),
        new ItemInfo("rlvnote", "Vendor notecard", "tops", "Notecard"),
        new ItemInfo("rlvlm", "Vendor LM", "tops", "Landmark"),
    });

    static ActionRow Move(string id, string from, string to, string name = "x") =>
        new(ActionKind.ItemMove, name, "Notecard", "", to, "test", id, from);

    static ActionRow TrashItem(string id, string from, string name = "x") =>
        new(ActionKind.ItemTrash, name, "Landmark", "", "", "test", id, from);

    static ActionRow TrashFolder(string id, string from, string name = "x") =>
        new(ActionKind.FolderTrash, name, "", "", "", "test", id, from);

    static CheckedAction One(ActionRow row, FakeTree? tree = null, ItemIndex? items = null) =>
        ActionChecker.Check(new[] { row }, tree ?? Sample(), items ?? Items()).Rows.Single();

    // ---- reading ----

    [Test]
    public void ReadsAllThreeKindsWithBomAndQuotes()
    {
        var csv = "﻿kind,item_name,asset_type,from,to,because,item_id,from_id\r\n" +
                  "item_move,\"Read, me\",Notecard,Pretty Dress,Notecards > Docs > Brand,docs,i1,f1\r\n" +
                  "item_trash,Vendor LM,Landmark,Pretty Dress,,ad,i2,f1\r\n" +
                  "folder_trash,Empty box,,Objects,,dupe,f9,f2\r\n";
        var rows = ActionPlanReader.Read(new StringReader(csv));
        Assert.That(rows.Select(r => r.Kind),
            Is.EqualTo(new[] { ActionKind.ItemMove, ActionKind.ItemTrash, ActionKind.FolderTrash }));
        Assert.That(rows[0].ItemName, Is.EqualTo("Read, me"));
        Assert.That((rows[0].To, rows[0].ItemId, rows[0].FromId), Is.EqualTo(("Notecards > Docs > Brand", "i1", "f1")));
        Assert.That(rows[2].ItemId, Is.EqualTo("f9"));
    }

    [Test]
    public void RejectsAnUnknownKindAndAMissingColumn()
    {
        var typo = "kind,item_name,asset_type,from,to,because,item_id,from_id\nitem_delete,x,,,,,i1,f1\n";
        var ex = Assert.Throws<InvalidDataException>(() => ActionPlanReader.Read(new StringReader(typo)));
        Assert.That(ex!.Message, Does.Contain("row 2").And.Contain("item_delete"));
        var noIds = "kind,item_name,asset_type,from,to,because,item_id\nitem_trash,x,,,,,i1\n";
        Assert.Throws<InvalidDataException>(() => ActionPlanReader.Read(new StringReader(noIds)));
    }

    [Test]
    public void LoadDetectsTheFormatFromTheHeader()
    {
        var actions = PlanFile.Read(new StringReader(
            ActionPlanReader.Header + "\nitem_trash,x,Landmark,a,,r,i1,f1\nitem_move,y,Notecard,a,B,r,i2,f1\n"));
        Assert.That(actions.Format, Is.EqualTo(PlanFormat.Actions));
        Assert.That(actions.Count, Is.EqualTo(2));
        Assert.That(actions.Describe(), Does.StartWith("2 item actions (1 item moves, 1 item trashes, 0 folder trashes)"));

        var folders = PlanFile.Read(new StringReader(
            "product_folder,items,from,to,because,creates_folder,folder_id,from_id\nP,1,x,Objects,r,,a1,r1\n"));
        Assert.That(folders.Format, Is.EqualTo(PlanFormat.Folders));
        Assert.That(folders.FolderRows.Single().FolderId, Is.EqualTo("a1"));
        Assert.That(folders.Describe(), Is.EqualTo("1 folder moves"));
    }

    [Test]
    public void AFolderPlanWithoutIdsIsStillRefused() =>
        Assert.Throws<InvalidDataException>(() =>
            PlanFile.Read(new StringReader("product_folder,items,from,to,because,creates_folder\nA,1,x,y,z,\n")));

    // ---- item_move ----

    [Test]
    public void MovesAnItemIntoAnExistingFolder()
    {
        var r = One(Move("note1", "prod", "Objects"));
        Assert.That((r.Outcome, r.DestinationId, r.CurrentParentId), Is.EqualTo((Outcome.Move, "objects", "prod")));
    }

    [Test]
    public void FilesNotecardsIntoTheExistingNotecardsSystemFolderAndCreatesTheRest()
    {
        var plan = ActionChecker.Check(new[]
        {
            Move("note1", "prod", "Notecards > Docs > Brand A"),
            Move("fresh", "inbox", "Notecards > Docs > Brand B"),
        }, Sample(), Items());
        Assert.That(plan.ItemMoves.Count(), Is.EqualTo(2));
        Assert.That(plan.ItemMoves.Select(m => m.DestinationId), Is.All.Null);
        Assert.That(plan.FoldersToCreate, Is.EqualTo(new[]
        {
            "Notecards > Docs",
            "Notecards > Docs > Brand A",
            "Notecards > Docs > Brand B",
        }));
        Assert.That(plan.Rows[0].Reason, Is.EqualTo("move (creates folders)"));
    }

    [Test]
    public void MovesIntoAnExistingSystemFolderDirectly()
    {
        var r = One(Move("note1", "prod", "Notecards"));
        Assert.That((r.Outcome, r.DestinationId), Is.EqualTo((Outcome.Move, "notecards")));
    }

    [Test]
    public void AllowsAnExistingProtectedDestinationButNeverCreatesInsideOne()
    {
        var existing = One(Move("note1", "prod", "#RLV > Tops"));
        Assert.That((existing.Outcome, existing.DestinationId), Is.EqualTo((Outcome.Move, "tops")));
        Assert.That(existing.Reason, Does.Contain("protected"));

        var creating = One(Move("note1", "prod", "#RLV > Tops > New"));
        Assert.That(creating.Outcome, Is.EqualTo(Outcome.Skip));
        Assert.That(creating.Reason, Does.StartWith("destination inside a protected folder"));
    }

    [Test]
    public void NeverCreatesAProtectedFolderUnasked()
    {
        var r = One(Move("note1", "prod", "Objects > RLV Spare"));
        Assert.That(r.Reason, Is.EqualTo("destination would create a protected folder"));
    }

    [Test]
    public void NeverFilesIntoTrashOrOtherSystemAreasAsAMove()
    {
        var r = One(Move("note1", "prod", "Trash"));
        Assert.That(r.Outcome, Is.EqualTo(Outcome.Skip));
        Assert.That(r.Reason, Does.StartWith("destination").And.Contain("Trash"));
    }

    [Test]
    public void AllowsClutterOutOfRlvButNotWearables()
    {
        var note = One(Move("rlvnote", "tops", "Notecards"));
        Assert.That((note.Outcome, note.Reason), Is.EqualTo((Outcome.Move, "move (out of #RLV: Notecard)")));
        var lm = One(TrashItem("rlvlm", "tops"));
        Assert.That((lm.Outcome, lm.Reason), Is.EqualTo((Outcome.Trash, "to Trash (out of #RLV: Landmark)")));
        var obj = One(new ActionRow(ActionKind.ItemTrash, "x", "Object", "", "", "t", "rlvitem", "tops"));
        Assert.That(obj.Outcome, Is.EqualTo(Outcome.Skip));
        Assert.That(obj.Reason, Does.Contain("protected folder").And.Contain("#RLV"));
    }

    [Test]
    public void SkipsWhenTheIndexTypeAndRowTypeDisagree()
    {
        var r = One(new ActionRow(ActionKind.ItemTrash, "x", "Landmark", "", "", "t", "rlvnote", "tops"));
        Assert.That(r.Outcome, Is.EqualTo(Outcome.Skip));
        Assert.That(r.Reason, Does.Contain("mismatch").And.Contain("Notecard"));
    }

    [Test]
    public void SkipsAnItemInAProtectedSource()
    {
        var fromRlv = One(Move("rlvitem", "tops", "Objects"));
        Assert.That(fromRlv.Outcome, Is.EqualTo(Outcome.Skip));
        Assert.That(fromRlv.Reason, Does.StartWith("item is inside a protected folder").And.Contain("#RLV"));

        var fromTrash = One(Move("trashed", "trash", "Objects"));
        Assert.That(fromTrash.Reason, Does.Contain("Trash"));
    }

    [Test]
    public void SkipsAMissingItemAndOneThatMovedOrIsAlreadyThere()
    {
        Assert.That(One(Move("gone", "prod", "Objects")).Reason, Is.EqualTo("no longer in the inventory"));
        Assert.That(One(Move("", "prod", "Objects")).Reason, Is.EqualTo("no longer in the inventory"));

        var moved = One(Move("fresh", "prod", "Objects"));
        Assert.That(moved.Outcome, Is.EqualTo(Outcome.Skip));
        Assert.That(moved.Reason, Does.StartWith("moved since the plan was made (now in Received Items)"));

        var there = One(Move("docnote", "prod", "Notecards"));
        Assert.That(there.Outcome, Is.EqualTo(Outcome.AlreadyThere));
    }

    [Test]
    public void SkipsARowWithNoDestinationAndADuplicateRow()
    {
        Assert.That(One(Move("note1", "prod", "")).Reason, Is.EqualTo("no destination"));
        var plan = ActionChecker.Check(new[]
        {
            Move("note1", "prod", "Objects"),
            Move("note1", "prod", "Notecards"),
            TrashFolder("old", "trash"),
        }, Sample(), Items());
        Assert.That(plan.Rows[1].Reason, Is.EqualTo("the plan lists this item twice"));
    }

    [Test]
    public void ARowForAnUnconfirmedFolderCountsAsMissing()
    {
        var tree = new ConfirmedTree(Sample(), new HashSet<string> { "objects", "trash", "prod" });
        var r = ActionChecker.Check(new[] { Move("note1", "prod", "Notecards") }, tree, Items()).Rows.Single();
        Assert.That(r.Outcome, Is.EqualTo(Outcome.Move));
        Assert.That(r.DestinationId, Is.Null);     // Notecards is not confirmed, so it is created
    }

    // ---- item_trash ----

    [Test]
    public void TrashesAnItemFromAnOrdinaryFolder()
    {
        var r = One(TrashItem("lm1", "prod"));
        Assert.That((r.Outcome, r.DestinationId, r.CurrentParentId), Is.EqualTo((Outcome.Trash, "trash", "prod")));
    }

    [Test]
    public void TrashesFromReceivedItemsButNotFromProtectedAreas()
    {
        Assert.That(One(TrashItem("fresh", "inbox")).Outcome, Is.EqualTo(Outcome.Trash));
        var r = One(TrashItem("rlvitem", "tops"));
        Assert.That(r.Outcome, Is.EqualTo(Outcome.Skip));
        Assert.That(r.Reason, Does.Contain("#RLV"));
    }

    [Test]
    public void RecognisesAnItemAlreadyInTrashEvenInASubfolder()
    {
        Assert.That(One(TrashItem("trashed", "trash")).Outcome, Is.EqualTo(Outcome.AlreadyThere));
        var deep = One(TrashItem("deep", "old"));
        Assert.That((deep.Outcome, deep.Reason), Is.EqualTo((Outcome.AlreadyThere, "already in Trash")));
    }

    [Test]
    public void TrashSkipsAMissingItemAMovedItemAndATreeWithoutTrash()
    {
        Assert.That(One(TrashItem("gone", "prod")).Reason, Is.EqualTo("no longer in the inventory"));
        Assert.That(One(TrashItem("lm1", "loose")).Reason, Does.StartWith("moved since the plan was made (now in Pretty Dress)"));
        var noTrash = new FakeTree().Add("prod", "Pretty Dress", "root");
        Assert.That(One(TrashItem("lm1", "prod"), noTrash).Reason, Is.EqualTo("no Trash folder found"));
    }

    // ---- folder_trash ----

    [Test]
    public void TrashesAnOrdinaryFolder()
    {
        var r = One(TrashFolder("hud", "prod"));
        Assert.That((r.Outcome, r.DestinationId, r.CurrentParentId), Is.EqualTo((Outcome.Trash, "trash", "prod")));
    }

    [Test]
    public void FolderTrashKeepsTheFolderMoveProtections()
    {
        Assert.That(One(TrashFolder("inbox", "root")).Reason, Does.Contain("system folder"));
        Assert.That(One(TrashFolder("trash", "root")).Reason, Does.Contain("system folder"));
        var inRlv = One(TrashFolder("tops", "rlv"));
        Assert.That(inRlv.Outcome, Is.EqualTo(Outcome.Skip));
        Assert.That(inRlv.Reason, Does.Contain("#RLV"));
        Assert.That(One(TrashFolder("rlv", "root")).Outcome, Is.EqualTo(Outcome.Skip));
        Assert.That(One(TrashFolder("gone", "root")).Reason, Is.EqualTo("no longer in the inventory"));
        Assert.That(One(TrashFolder("hud", "loose")).Reason, Does.StartWith("moved since the plan was made"));
    }

    [Test]
    public void AFolderAlreadyInTrashIsAlreadyThere()
    {
        var r = One(TrashFolder("old", "trash"));
        Assert.That((r.Outcome, r.Reason), Is.EqualTo((Outcome.AlreadyThere, "already in Trash")));
    }

    // ---- folder_trash of empty folders inside #RLV ----

    static FakeTree EmptiesTree() => Sample()
        .Add("rlvempty", "Cuffs", "rlv")
        .Add("rlvouter", "Clothing", "rlv")
        .Add("rlvinner", "Dresses", "rlvouter")
        .Add("rlvfull", "Armbinders", "rlv")
        .Add("rlvdeep", "Parts", "rlvfull")
        .Add("rlvdeeper", "Docs", "rlvdeep")
        .Add("vendor", "~vendor", "rlv")
        .Add("vendorsub", "Empty part", "vendor");

    static ItemIndex EmptiesItems() => new(new[] { new ItemInfo("deepitem", "Strap", "rlvdeeper", "Object") });

    [Test]
    public void AnEmptyFolderInsideRlvIsTrashed()
    {
        var r = One(TrashFolder("rlvempty", "rlv"), EmptiesTree(), EmptiesItems());
        Assert.That((r.Outcome, r.Reason, r.DestinationId), Is.EqualTo((Outcome.Trash, "to Trash (empty, inside #RLV)", "trash")));
    }

    [Test]
    public void AFolderWhoseOnlyContentIsAnEmptySubfolderIsTrashed()
    {
        Assert.That(One(TrashFolder("rlvouter", "rlv"), EmptiesTree(), EmptiesItems()).Outcome, Is.EqualTo(Outcome.Trash));
    }

    [Test]
    public void AFolderWithAnItemAnywhereBelowIsRefused()
    {
        var r = One(TrashFolder("rlvfull", "rlv"), EmptiesTree(), EmptiesItems());
        Assert.That(r.Outcome, Is.EqualTo(Outcome.Skip));
        Assert.That(r.Reason, Does.Contain("#RLV"));
    }

    [Test]
    public void VendorManagedAndRlvItselfStayRefusedEvenWhenEmpty()
    {
        Assert.That(One(TrashFolder("vendorsub", "vendor"), EmptiesTree(), EmptiesItems()).Outcome, Is.EqualTo(Outcome.Skip));
        Assert.That(One(TrashFolder("vendor", "rlv"), EmptiesTree(), EmptiesItems()).Outcome, Is.EqualTo(Outcome.Skip));
        Assert.That(One(TrashFolder("rlv", "root"), EmptiesTree(), new ItemIndex()).Outcome, Is.EqualTo(Outcome.Skip));
    }

    [Test]
    public void IsEmptyRecursiveLooksAtTheWholeSubtree()
    {
        var t = EmptiesTree(); var i = EmptiesItems();
        Assert.That(FolderContents.IsEmptyRecursive(t, i, "rlvouter"), Is.True);
        Assert.That(FolderContents.IsEmptyRecursive(t, i, "rlvfull"), Is.False);
    }

    // ---- preview and summary ----

    [Test]
    public void SummaryAndPreviewUseTheSameVocabulary()
    {
        var plan = ActionChecker.Check(new[]
        {
            Move("note1", "prod", "Notecards > Docs"),
            TrashItem("lm1", "prod", "Vendor, LM"),
            TrashFolder("hud", "prod", "HUD"),
            Move("docnote", "prod", "Notecards"),
            Move("gone", "prod", "Objects"),
        }, Sample(), Items());
        Assert.That(plan.Summary(), Is.EqualTo(
            "1 item moves, 1 items to Trash, 1 folders to Trash, 1 new folders, 1 already in place, 1 skipped"));
        Assert.That(plan.Todo.Count(), Is.EqualTo(3));

        var w = new StringWriter();
        plan.WritePreview(w);
        var lines = Csv.Parse(new StringReader(w.ToString()));
        Assert.That(lines[0], Is.EqualTo(new[] { "outcome", "kind", "item_name", "asset_type", "from", "to", "reason", "item_id" }));
        Assert.That(lines.Skip(1).Select(l => l[0]),
            Is.EqualTo(new[] { "Move", "Trash", "Trash", "AlreadyThere", "Skip", "CreateFolder" }));
        Assert.That(lines[2][2], Is.EqualTo("Vendor, LM"));
        Assert.That(lines[2][5], Is.EqualTo("Trash"));
        Assert.That(lines.Last()[5], Is.EqualTo("Notecards > Docs"));
        Assert.That(plan.SkipReasons.Single(), Is.EqualTo("no longer in the inventory"));
    }

    // ---- journal ----

    static JournalEntry Entry(string item, string from, string to, string name = "x") =>
        new(DateTime.UtcNow, JournalKind.Item, item, from, to, name);

    [Test]
    public void JournalRoundTripsItemEntries()
    {
        var path = Path.GetTempFileName();
        File.Delete(path);
        try
        {
            using (var j = new Journal(path))
            {
                j.Write(Entry("note1", "prod", "objects", "Read\tme"));
                j.Write(new JournalEntry(DateTime.UtcNow, JournalKind.Move, "hud", "prod", "trash", "HUD"));
            }
            List<JournalEntry> entries;
            using (var r = new StreamReader(path)) entries = Journal.Read(r);
            Assert.That(entries.Select(e => e.Kind), Is.EqualTo(new[] { JournalKind.Item, JournalKind.Move }));
            Assert.That((entries[0].FolderId, entries[0].OldParentId, entries[0].NewParentId, entries[0].Name),
                Is.EqualTo(("note1", "prod", "objects", "Read me")));

            // The folder undo ignores item entries, and the item undo ignores folder entries.
            var tree = Sample();
            tree.Move("hud", "trash");
            var (folderMoves, _, _, _) = Journal.PlanUndoAll(entries, tree);
            Assert.That(folderMoves.Select(m => m.FolderId), Is.EqualTo(new[] { "hud" }));
            var (itemMoves, _) = Journal.PlanUndoItems(entries, new ItemIndex(new[] { new ItemInfo("note1", "Read me", "objects", "Notecard") }), tree);
            Assert.That(itemMoves.Select(m => m.FolderId), Is.EqualTo(new[] { "note1" }));
        }
        finally { File.Delete(path); }
    }

    [Test]
    public void UndoMovesAnItemBackAndOutOfTrash()
    {
        var entries = new List<JournalEntry>
        {
            Entry("note1", "prod", "notecards", "Read me"),
            Entry("lm1", "prod", "trash", "Vendor LM"),
        };
        var items = new ItemIndex(new[]
        {
            new ItemInfo("note1", "Read me", "notecards", "Notecard"),
            new ItemInfo("lm1", "Vendor LM", "trash", "Landmark"),
        });
        var (moves, skipped) = Journal.PlanUndoItems(entries, items, Sample());
        Assert.That(skipped, Is.Empty);
        // Newest first; each goes from where it is now back to where it came from.
        Assert.That(moves.Select(m => (m.FolderId, m.OldParentId, m.NewParentId)),
            Is.EqualTo(new[] { ("lm1", "trash", "prod"), ("note1", "notecards", "prod") }));
    }

    [Test]
    public void UndoSkipsWhatMovedAgainWhatIsGoneAndWhatLostItsOldFolder()
    {
        var entries = new List<JournalEntry>
        {
            Entry("note1", "prod", "notecards", "Moved by hand"),
            Entry("lm1", "prod", "trash", "Gone"),
            Entry("fresh", "gonefolder", "objects", "No home"),
            Entry("docnote", "prod", "notecards", "Fine"),
        };
        var items = new ItemIndex(new[]
        {
            new ItemInfo("note1", "x", "objects", "Notecard"),       // moved again since
            new ItemInfo("fresh", "x", "objects", "Object"),         // old folder is gone
            new ItemInfo("docnote", "x", "notecards", "Notecard"),
        });
        var (moves, skipped) = Journal.PlanUndoItems(entries, items, Sample());
        Assert.That(moves.Select(m => m.FolderId), Is.EqualTo(new[] { "docnote" }));
        Assert.That(skipped, Has.Count.EqualTo(3));
        Assert.That(skipped[0], Does.Contain("moved again"));
        Assert.That(skipped[1], Does.Contain("no longer in the inventory"));
        Assert.That(skipped[2], Does.Contain("old folder is gone"));
    }

    [Test]
    public void AnItemMovedTwiceGoesBackToWhereItFirstCameFrom()
    {
        var entries = new List<JournalEntry>
        {
            Entry("note1", "prod", "notecards", "Read me"),
            Entry("note1", "notecards", "objects", "Read me"),
        };
        var items = new ItemIndex(new[] { new ItemInfo("note1", "Read me", "objects", "Notecard") });
        var (moves, skipped) = Journal.PlanUndoItems(entries, items, Sample());
        Assert.That(skipped, Is.Empty);
        Assert.That(moves.Single(), Is.EqualTo(entries[1] with { OldParentId = "objects", NewParentId = "prod" }));
    }

    [Test]
    public void ItemIndexTracksMoves()
    {
        var items = Items();
        items.SetParent("note1", "objects");
        items.SetParent("nobody", "objects");      // unknown ids are ignored
        Assert.That(items.GetItem("note1")!.ParentId, Is.EqualTo("objects"));
        Assert.That(items.GetItem("nobody"), Is.Null);
    }

    [Test]
    public void ActionSummaryNamesEveryCount() =>
        Assert.That(ApplyReport.ActionSummary(5, 3, 2, 1, 4),
            Is.EqualTo("apply finished: moved 5 items, trashed 3 items and 2 folders, failed 1, created 4"));
}
