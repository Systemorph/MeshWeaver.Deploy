using System.IO;
using System.Linq;
using System.Text.RegularExpressions;
using MeshWeaver.Deployment;
using Xunit;

namespace MeshWeaver.Deploy.ChartGuards;

/// <summary>
/// What a record can and cannot SAY about the modules an instance requires (#4476) — the one fact
/// that reads the CHART, moved here with it. Everything else in the platform's
/// <c>MeshWeaver.Deployment.Contract.Test.RequiredModuleAuthorityTest</c> pins the contract's own
/// rendering and stayed there.
/// </summary>
public class RequiredModuleAuthorityTest
{
    /// <summary>
    /// 🚨 The chart's <c>Modules__Required__N</c> block names every key LITERALLY, so it has a
    /// hand-written ceiling; a slot above it reaches no container in Kubernetes. That was merely
    /// wrong while the entries were a by-index overlay (the image's entry stood); under the
    /// authority claim it means the module is NOT REQUIRED AT ALL. So the number the contract
    /// reports problems against has to be the number the template actually renders — read from the
    /// template, not restated.
    ///
    /// <para>The platform copy skipped when the repository tree was not reachable from the test
    /// bin. Here the chart is this repository's own, so a missing template FAILS: the contract's
    /// ceiling would otherwise go unchecked against the only template that renders it.</para>
    /// </summary>
    [Fact]
    public void TheDeclaredCeilingIsTheOneTheChartRenders()
    {
        var chart = ChartRoots.ChartFile("deploy/helm/templates/memex-portal/config.yaml");

        var rendered = Regex.Matches(File.ReadAllText(chart), @"^\s*Modules__Required__(\d+):", RegexOptions.Multiline)
            .Select(m => int.Parse(m.Groups[1].Value))
            .Distinct()
            .Order()
            .ToArray();

        Assert.True(rendered.Length > 0, "the chart renders no Modules__Required__N key at all");
        Assert.Equal(0, rendered[0]);
        Assert.Equal(Enumerable.Range(0, rendered[^1] + 1), rendered);
        Assert.Equal(DeploymentPortalConfig.MaxChartRenderedRequiredModuleSlot, rendered[^1]);
    }
}
