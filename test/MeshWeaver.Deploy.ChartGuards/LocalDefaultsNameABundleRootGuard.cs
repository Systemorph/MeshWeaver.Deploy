using System;
using System.IO;
using System.Linq;
using Xunit;

namespace MeshWeaver.Deploy.ChartGuards;

/// <summary>
/// The local instance must name a bundle root — adopt-only with nothing to adopt from reports every
/// dynamic type as uncovered forever, which is honest but useless.
/// <para>🏠 Moved from the platform's <c>PlatformBakeLaneGuard</c> with memex-local's overlays
/// (core slimming); the rest of that guard is about the platform's own bake lane and stayed.</para>
/// </summary>
public class LocalDefaultsNameABundleRootGuard
{
    [Fact]
    public void LocalDefaults_NameABundleRootToAdoptFrom()
    {
        var values = File.ReadAllLines(ChartRoots.ChartFile("deploy/homebrew/share/values.local.defaults.yaml"))
            .Select(l => l.Trim())
            .Where(l => !l.StartsWith('#'))
            .ToList();

        Assert.True(
            values.Any(l => l.StartsWith("PreWarm__PrebuiltBundleRoot:", StringComparison.Ordinal)
                            && l.Contains('/', StringComparison.Ordinal)),
            "The local instance must name a bundle root, otherwise adopt-only has no bundles to "
            + "adopt from and reports every dynamic type as uncovered forever.");
    }
}
