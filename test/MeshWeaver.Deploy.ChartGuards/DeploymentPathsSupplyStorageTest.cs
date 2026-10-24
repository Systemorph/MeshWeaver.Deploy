using System.IO;
using System.Text.RegularExpressions;
using Xunit;

namespace MeshWeaver.Deploy.ChartGuards;

/// <summary>
/// Every path that deploys a PORTAL must state its storage explicitly — the CHART half.
///
/// <para>🚨 <b>This is the dangerous direction of the first-run setup change, and it is the one
/// with a production outage on the other side.</b> The deployed image deliberately no longer bakes
/// a <c>Graph:Storage:Type</c> default — that default made the setup wizard unreachable on
/// Kubernetes (<see cref="DeployedImageDoesNotPreAnswerStorageTest"/>). The safety of removing it
/// rests ENTIRELY on the deployment paths supplying the value themselves. If one of them ever
/// stops, a configured production portal with real data reboots into a FIRST-RUN SETUP WIZARD,
/// serving no content and offering a stranger the chance to configure it — and nothing fails,
/// because "no storage configured" is a legitimate state by design.</para>
///
/// <para>Moved here with the chart. The Aspire half (<c>TheAspireHostStatesTheStorageType</c>) reads
/// no chart — it pins the record's portal-config derivation — and stayed in the platform's
/// Memex.Portal.Shared.Test.</para>
/// </summary>
public class DeploymentPathsSupplyStorageTest
{
    [Fact]
    public void TheHelmChartStatesTheStorageType()
    {
        // The AKS lane. Every portal the chart deploys reads Graph__Storage__Type from its
        // ConfigMap, and the ConfigMap emits the key only when the values state it.
        var values = Path.Combine(ChartRoots.DeployRoot(), "deploy", "helm", "values.yaml");
        Assert.True(File.Exists(values), $"the chart's values are not at {values}");

        var match = Regex.Match(File.ReadAllText(values),
            @"^\s*Graph__Storage__Type:\s*""(?<v>[^""]*)""", RegexOptions.Multiline);

        Assert.True(match.Success,
            $"{values} no longer states Graph__Storage__Type. The deployed image does not carry a "
            + "default any more, so every portal this chart deploys would boot into the first-run "
            + "SETUP WIZARD instead of serving its data.");
        Assert.False(string.IsNullOrWhiteSpace(match.Groups["v"].Value),
            "Graph__Storage__Type is present but EMPTY — the ConfigMap omits an empty key, which is "
            + "indistinguishable from not stating it at all.");
    }
}
