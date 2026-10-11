using System;
using System.Collections.Generic;
using NUnit.Framework;
using Veles.Plugin.Hangar.Core;

namespace Veles.Plugin.Hangar.Tests;

public class FetchBatchTests
{
    [TestCase(0, 8, 20, 0)]
    [TestCase(1, 8, 20, 1)]
    [TestCase(8, 8, 20, 1)]
    [TestCase(9, 8, 20, 2)]
    [TestCase(100, 8, 20, 13)]
    [TestCase(160, 8, 20, 20)]
    [TestCase(5000, 8, 20, 20)]
    public void NextSize_shares_the_queue_between_workers(int queued, int workers, int max, int expected) =>
        Assert.That(FetchBatches.NextSize(queued, workers, max), Is.EqualTo(expected));

    [Test]
    public void NextSize_rejects_bad_limits()
    {
        Assert.Throws<ArgumentOutOfRangeException>(() => FetchBatches.NextSize(5, 0, 20));
        Assert.Throws<ArgumentOutOfRangeException>(() => FetchBatches.NextSize(5, 8, 0));
    }

    [Test]
    public void Missing_keeps_request_order()
    {
        var got = FetchBatches.Missing(new[] { "a", "b", "c", "d" }, new HashSet<string> { "b", "d" });
        Assert.That(got, Is.EqualTo(new[] { "a", "c" }));
    }

    [Test]
    public void Missing_is_empty_when_all_answered() =>
        Assert.That(FetchBatches.Missing(new[] { "a" }, new HashSet<string> { "a", "x" }), Is.Empty);

    [TestCase(0, 0, 0, true)]
    [TestCase(5, 2, 3, true)]
    [TestCase(5, 2, 2, false)]
    [TestCase(0, 1, 0, false)]
    public void CountsAgree_compares_listing_to_descendents(int d, int f, int i, bool ok) =>
        Assert.That(FetchBatches.CountsAgree(d, f, i), Is.EqualTo(ok));

    [TestCase(0, 0, true)]
    [TestCase(5, 0, false)]
    [TestCase(5, 1, true)]
    public void Plausible_needs_one_answer(int requested, int answered, bool ok) =>
        Assert.That(FetchBatches.Plausible(requested, answered), Is.EqualTo(ok));
}
