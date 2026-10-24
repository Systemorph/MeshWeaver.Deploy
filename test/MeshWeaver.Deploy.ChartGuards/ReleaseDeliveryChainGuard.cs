using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using MeshWeaver.Graph.Configuration;
using Xunit;

namespace MeshWeaver.Deploy.ChartGuards;

/// <summary>
/// Governance guard for the release-broadcast chain (<c>#2235</c>) — CD's promoted build →
/// the control instance's webhook inbox → HMAC verify → one <c>repository_dispatch</c> per
/// subscriber. Every joint of that chain is switched on by a CONFIGURATION KEY, and every one of
/// them is silent when it is off: an unlisted inbox target answers 404 (identical to a wrong URL),
/// an empty subscriber set reports <c>0 dispatched, 0 failed</c> (identical to a mesh that is not
/// the control instance).
///
/// <para>🚨 <b>The failure this file exists for.</b> <c>FrameworkBroadcast:Subscribers</c> — then
/// the only source of the subscriber set — was rendered by <b>no chart in either repo</b> for the
/// whole life of the feature, so no deployment could set it. Every broadcast ran against an empty
/// set and logged it at <c>Information</c> as normal. Nothing was red, because a key that no chart
/// renders cannot be distinguished from a key a deployment chose to leave empty.</para>
///
/// <para>Moved here with the chart: this is the ledger's CHART fact. The platform's
/// <c>MeshWeaver.Documentation.Test.ReleaseDeliveryChainGuard</c> keeps the prose fact (the CD
/// workflow and the CD contract name the subscriber source that exists) and the Key Vault template
/// fact, whose subject (<c>deploy/aks/envs/example</c>) was not part of the chart move.</para>
/// </summary>
public class ReleaseDeliveryChainGuard
{
    private const string ConfigMap = "deploy/helm/templates/memex-portal/config.yaml";
    private const string ValuesAks = "deploy/aks/values.aks.yaml";

    /// <summary>
    /// One joint of the delivery chain: the configuration key that switches it on, in the
    /// double-underscore environment form a deployment actually sets, plus what silently does not
    /// happen while the key cannot be set at all.
    /// </summary>
    /// <param name="Joint">The joint's name, for the failure message.</param>
    /// <param name="EnvKeyPrefix">The env-var prefix; slots are this plus <c>0..Slots-1</c>.</param>
    /// <param name="Slots">
    /// How many indexed slots a deployment must be able to use. 🚨 EVERY slot is asserted, not just
    /// <c>__0</c> — this repo's own incident is the reason. memex-cloud cannot use
    /// <c>WebhookInbox__Targets__0</c> at all (a stray inline <c>env:</c> shadows it permanently),
    /// so the release target lives on <c>__1</c>; a guard that only checked <c>__0</c> would stay
    /// green while the one slot the control instance actually depends on disappeared from the
    /// chart, re-creating the identical silent misconfiguration one index over.
    /// </param>
    /// <param name="Consequence">What is silently lost when no deployment can set the key.</param>
    /// <param name="EnvKeySuffix">Appended after the slot index, for keys that are a CHILD of a
    /// slot rather than the slot itself (<c>…__0__SecretConfigKey</c>).</param>
    private sealed record ChainKey(
        string Joint, string EnvKeyPrefix, int Slots, string Consequence, string EnvKeySuffix = "");

    /// <summary>
    /// The chain's keys, each derived from the CONSTANT its reader owns — never a literal. A
    /// literal would restate the key, and the guard would then go green through exactly the rename
    /// it is here to catch (AGENTS.md: a renamed config key is a silent deletion).
    /// </summary>
    private static IEnumerable<ChainKey> ChainKeys()
    {
        // Joint 1 — CD's signed POST is accepted at all. The inbox is fail-closed: a target
        // missing from this allowlist answers 404, byte-identical to a wrong URL. TWO slots,
        // because index 0 is not the chart's alone to give: the control instance's live Deployment
        // carries a hand-set inline `env:` for __0 that beats `envFrom` forever, which is why the
        // release target had to move to __1 (#2352). __1 is the slot that actually carries it.
        yield return new ChainKey(
            "the webhook inbox accepts the release event",
            EnvForm(WebhookInbox.TargetsConfigSection) + "__", 2,
            "CD's notify-platform-update 404s on every promoted build and no release event ever "
            + "reaches the mesh");

        // Joint 1b — the inbox actually CHECKS the signature on that target (#3312). Without this
        // key the endpoint keeps the dumb contract: it stores the delivery and answers 2xx
        // whatever the HMAC says, so a secret that has drifted between CD and the instance is
        // byte-identical to one that matches — the exact state this ledger exists to make
        // impossible. Both slots, for the same reason as above: the slot carrying
        // Hosting/PlatformBuilds is not the same index on every deployment, and a declaration
        // rendered for only __0 would leave the control instance's real slot undeclarable.
        //
        // 🚨 This asserts the key is RENDERABLE, not that any deployment sets it — and it must not
        // be strengthened into "…is non-empty in values". An empty declaration is CORRECT for a
        // slot holding a Stripe target: Stripe signs `Stripe-Signature`, and requiring this
        // scheme there would 401 every payment delivery. What catches a MISSING declaration on the
        // slot that needs one is the publishing lane reading the inbox's answer
        // (`signature: not-required` → ::warning::), not this file.
        yield return new ChainKey(
            "the webhook inbox VERIFIES the release event's signature",
            EnvForm(WebhookInbox.TargetsConfigSection) + "__", 2,
            "the inbox accepts every delivery to that target unverified, so a drifted "
            + "PLATFORM_WEBHOOK_SECRET answers 2xx and is dropped in silence downstream",
            EnvKeySuffix: "__" + WebhookInbox.SecretConfigKeyName);

        // Joint 2 — the verified release fans out — is NOT a configuration key any more. Since
        // 2026-09-03 the subscriber set is data in the mesh (the Hosting/Deployment records'
        // registry-source mounts, derived by PlatformBuildInboxWatcher), so there is nothing for a
        // chart to render and nothing for a deployment to leave blank. The retired
        // FrameworkBroadcast__Subscribers__N slots must NOT come back here: a guard asserting a key
        // nothing reads is a false control.
    }

    /// <summary>
    /// 🚨 THE CONTROL. A key the code READS and no chart RENDERS cannot be set by any deployment,
    /// and its feature is therefore permanently off — while reading exactly like a feature a
    /// deployment left off on purpose. Both ends are asserted: the ConfigMap must name the key
    /// (the Deployment's only env path is <c>envFrom</c> on it, and it carries no catch-all range),
    /// and a values file must declare the slot so an overlay that sets it has something to override.
    /// </summary>
    [Fact]
    public void EveryConfigKeyTheReleaseChainReads_IsRenderedByTheChart()
    {
        var configMap = File.ReadAllText(ChartRoots.ChartFile(ConfigMap));
        var values = File.ReadAllText(ChartRoots.ChartFile(ValuesAks));

        var broken = ChainKeys()
            .SelectMany(k => Enumerable.Range(0, k.Slots)
                .Select(i => (k, EnvKey: k.EnvKeyPrefix + i + k.EnvKeySuffix)))
            .Select(x => (x.k, x.EnvKey,
                inConfigMap: RendersKey(configMap, x.EnvKey), inValues: DeclaresKey(values, x.EnvKey)))
            .Where(x => !x.inConfigMap || !x.inValues)
            .ToList();

        Assert.True(broken.Count == 0,
            "These release-delivery-chain keys cannot be set by ANY deployment — the code reads "
            + "them and the chart does not render them, so the joint they switch on is "
            + "permanently off and indistinguishable from one a deployment turned off:\n"
            + string.Join("\n", broken.Select(x =>
                $"  • {x.EnvKey} ({x.k.Joint}) — "
                + (x.inConfigMap ? "" : $"not rendered by {ConfigMap}; ")
                + (x.inValues ? "" : $"no slot declared in {ValuesAks}; ")
                + $"consequence: {x.k.Consequence}")));
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    /// <summary>A configuration path (<c>A:B</c>) in the env-var form a container receives.</summary>
    private static string EnvForm(string configPath) => configPath.Replace(":", "__", StringComparison.Ordinal);

    /// <summary>
    /// The ConfigMap renders the key when it appears as a mapping key whose value interpolates the
    /// SAME name out of <c>config.memex_portal</c>. Checking both halves is deliberate: a line that
    /// names the key but interpolates a different one is the #1925 shape wearing the right label.
    /// </summary>
    private static bool RendersKey(string configMap, string key) =>
        configMap.Split('\n').Any(l =>
        {
            var t = l.Trim();
            return !t.StartsWith('#')
                   && t.StartsWith(key + ":", StringComparison.Ordinal)
                   && t.Contains("config.memex_portal." + key, StringComparison.Ordinal);
        });

    /// <summary>A values file declares the slot when it carries the key as a mapping key.</summary>
    private static bool DeclaresKey(string values, string key) =>
        values.Split('\n').Any(l =>
        {
            var t = l.Trim();
            return !t.StartsWith('#') && t.StartsWith(key + ":", StringComparison.Ordinal);
        });
}
