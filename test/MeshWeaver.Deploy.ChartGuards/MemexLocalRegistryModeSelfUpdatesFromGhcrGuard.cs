using System;
using System.IO;
using System.Linq;
using Xunit;

namespace MeshWeaver.Deploy.ChartGuards;

/// <summary>
/// 🚨 A memex-local install in REGISTRY MODE self-updates from the PUBLIC GHCR, with no credential
/// (MeshWeaver.Deploy#65) — asserted by RENDERING the chart with the tracked registry-mode layer the
/// CLI passes, not by reading the layer.
///
/// <para><b>Why this guard exists.</b> In registry mode the CLI used to set no <c>selfUpdate.*</c>
/// value at all, so the portal kept the image default — the upstream ACR, listed through a managed
/// identity. A Mac has none, so every check failed with <c>ManagedIdentityCredential authentication
/// failed</c>, the install never rolled its platform, and every package whose floor was above it was
/// held. The fix is three chart values, and each one is load-bearing: the registry the install
/// already pulls from, the declaration that it is public (so the self-updater lists anonymously and
/// never presents the instance key there), and <c>canPatch</c> (a standalone local cluster has no
/// control instance to hand a release to). The repositories are DERIVED by the chart from the
/// images the release runs, so the render must show <c>systemorph/memex-portal-ai</c> — a listing of
/// the bare default <c>memex-portal-ai</c> on ghcr.io names a repository that does not exist.</para>
///
/// <para><b>Negative control.</b> The chart defaults render <c>SelfUpdate__RegistryIsPublic</c> as
/// <c>false</c> and no <c>SelfUpdate__Registry</c>, so the keyed rule stays exactly as it was for every
/// install that does not opt in; against the chart before this change the registry-mode render has
/// no <c>SelfUpdate__RegistryIsPublic</c> line and the first fact fails.</para>
/// </summary>
public class MemexLocalRegistryModeSelfUpdatesFromGhcrGuard
{
    private const string RegistryModeLayer = "deploy/homebrew/share/values.local.registry-mode.yaml";
    private const string Cli = "deploy/homebrew/bin/memex-local";

    [Fact(Timeout = 120000)]
    public void TheRegistryModeLayer_RendersAPublicGhcrSelfUpdate_WithTheDerivedRepositories()
    {
        TestContext.Current.CancellationToken.ThrowIfCancellationRequested();
        var layer = File.ReadAllText(ChartRoots.ChartFile(RegistryModeLayer));

        var data = AutomationSwitchesReachTheConfigMapGuard.RenderConfigMapData(layer);

        data.Should().ContainKey("SelfUpdate__RegistryIsPublic",
            "the declaration that the registry is public must reach the pod, or the self-updater takes "
            + "the keyed path and refuses ghcr.io for want of a key");
        data["SelfUpdate__RegistryIsPublic"].Value.Should().Be("true");
        data["SelfUpdate__Registry"].Value.Should().Be("ghcr.io",
            "the install pulls ghcr.io/systemorph/memex-portal-ai and must list the registry it pulls from");
        data["SelfUpdate__PortalRepository"].Value.Should().Be("systemorph/memex-portal-ai",
            "GHCR's repository carries the organisation; it is derived from portal.image, never the bare default");
        data["SelfUpdate__MigrationRepository"].Value.Should().Be("systemorph/memex-migration",
            "the migration rolls to the same tag from the same public registry");
        data["SelfUpdate__CanPatch"].Value.Should().Be("true",
            "a standalone local cluster has no control instance to hand the release to");
        data["SelfUpdate__RegistryValidationUrl"].Value.Should().Be("",
            "a public registry validates no key, so no validator is declared");
    }

    [Fact(Timeout = 120000)]
    public void TheChartDefaults_DeclareNoPublicRegistry()
    {
        TestContext.Current.CancellationToken.ThrowIfCancellationRequested();

        var data = AutomationSwitchesReachTheConfigMapGuard.RenderConfigMapData(null);

        data["SelfUpdate__RegistryIsPublic"].Value.Should().Be("false",
            "an install that does not opt in keeps the keyed rule — public is a declaration, never a default");
        data.Should().NotContainKey("SelfUpdate__Registry",
            "the chart default keeps the image's own registry default");
    }

    [Fact]
    public void TheRegistryModeLayer_NamesNoPullSecret_AndIsPassedOnlyInRegistryMode()
    {
        var layer = File.ReadAllLines(ChartRoots.ChartFile(RegistryModeLayer))
            .Select(l => l.Trim())
            .Where(l => l.Length > 0 && !l.StartsWith('#'))
            .ToList();
        layer.Should().NotContain(l => l.Contains("imagePullSecret", StringComparison.Ordinal),
            "a public registry needs no pull credential, and none must be configured for it");

        var cli = File.ReadAllText(ChartRoots.ChartFile(Cli));
        var helmDeploy = cli[cli.IndexOf("helm_deploy() {", StringComparison.Ordinal)..];
        helmDeploy = helmDeploy[..helmDeploy.IndexOf("\n}\n", StringComparison.Ordinal)];
        var registryBranch = helmDeploy[helmDeploy.IndexOf("if registry_mode; then", StringComparison.Ordinal)..];
        registryBranch = registryBranch[..registryBranch.IndexOf("else", StringComparison.Ordinal)];

        registryBranch.Should().Contain("values.local.registry-mode.yaml",
            "helm_deploy must layer the registry-mode self-update values in registry mode");
        registryBranch.IndexOf("values.local.registry-mode.yaml", StringComparison.Ordinal)
            .Should().BeLessThan(registryBranch.IndexOf("\"$REGISTRY_FILE\"", StringComparison.Ordinal),
                "the tracked layer goes first so the per-install registry file, and then the overlay, can override it");
        helmDeploy.Split("values.local.registry-mode.yaml").Length.Should().Be(2,
            "the layer is passed in exactly one place — the registry-mode branch");
    }
}
