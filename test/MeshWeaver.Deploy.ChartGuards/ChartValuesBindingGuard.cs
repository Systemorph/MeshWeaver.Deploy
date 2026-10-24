using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text.RegularExpressions;
using Xunit;

namespace MeshWeaver.Deploy.ChartGuards;

/// <summary>
/// The values-file ↔ ConfigMap half of the platform's <c>PlatformBakeLaneGuard</c>, moved here
/// with the chart it reads (the platform repository carries no deployment knowledge). Every fact
/// is the original, assertion for assertion and message for message; only the roots changed:
/// <list type="bullet">
///   <item>the chart — <c>deploy/helm</c>, <c>deploy/aks/values.aks.yaml</c>,
///     <c>deploy/aks/scripts/values.deploy.example.yaml</c> — resolves against THIS repository and
///     must exist (it moved here; a missing chart is a moved subject, never a skip);</item>
///   <item>the memex-local overlays the chart is ALSO rendered with —
///     <c>deploy/homebrew/share/values.local*.yaml</c> — moved here with memex-local, so they resolve
///     against THIS repository too, and keep the original's tolerance of an absent file.</item>
/// </list>
/// </summary>
public class ChartValuesBindingGuard
{
    /// <summary>
    /// 🚨 A <c>PreWarm__*</c> key set in a values file but NOT templated in the configmap is
    /// SILENTLY DROPPED — the deployment runs as if it were never configured, with no warning from
    /// helm, kubectl, or the portal.
    ///
    /// <para>This is not hypothetical. <c>deploy/aks/values.aks.yaml</c> set
    /// <c>PreWarm__PrebuiltBundleRoot: "/data/prebuilt-bundles"</c> from the day the CI-published
    /// bake lane shipped (#1660 WS3), while <c>config.yaml</c> never rendered the key — so every
    /// chart-deployed portal ran with the consuming half of that lane inert and Roslyn-compiled the
    /// content CI had already baked for it. The producer worked, the value was right, and the one
    /// line that would have carried it across did not exist.</para>
    ///
    /// <para>The configmap enumerates keys explicitly (it does not iterate <c>.Values.config</c>),
    /// which is a deliberate choice — it keeps the deployed surface reviewable — but it means
    /// adding a key to a values file is only ever HALF the change. This guard is the other half,
    /// asserted rather than remembered.</para>
    /// </summary>
    [Fact]
    public void EveryPreWarmKeyInValues_IsTemplatedInTheConfigMap()
    {
        var configMap = ReadPortalConfigMap();

        // The values files the chart is actually deployed with. A key is "set" when it appears as a
        // mapping key; comments are excluded so the prose describing a key never counts as using it.
        var valuesFiles = new[]
        {
            Chart("deploy/helm/values.yaml"),
            Chart("deploy/aks/values.aks.yaml"),
            Local("values.local.defaults.yaml"),
            Local("values.local.self-registry.yaml"),
        };

        var missing = valuesFiles
            .Where(File.Exists)
            .SelectMany(f => File.ReadAllLines(f)
                .Select(l => l.Trim())
                .Where(l => !l.StartsWith('#') && l.StartsWith("PreWarm__", StringComparison.Ordinal))
                .Select(l => l[..l.IndexOf(':', StringComparison.Ordinal)].Trim())
                .Select(key => (file: Path.GetFileName(f), key)))
            .Distinct()
            // 🚨 Templated means the key is BOUND, not merely mentioned. Matching "<key>:" alone
            // would be satisfied by the explanatory Helm comment blocks that precede each entry —
            // they are `{{- /* ... */}}`, which no '#'-stripping can remove, and several of them
            // open with the very key they describe. A check a comment can satisfy is not a check.
            // Requiring the value binding as well is a shape prose cannot accidentally have.
            .Where(x => !IsBoundInConfigMap(configMap, x.key))
            .ToList();

        Assert.True(missing.Count == 0,
            "These PreWarm keys are set in a values file but never rendered by "
            + "deploy/helm/templates/memex-portal/config.yaml, so the deployment silently ignores "
            + "them: "
            + string.Join(", ", missing.Select(x => $"{x.key} (set in {x.file})"))
            + ". Add each one to the configmap template — the values file alone does nothing.");
    }

    /// <summary>
    /// Keys a values file sets that reach the portal by a route OTHER than this configmap, and are
    /// therefore legitimately unbound here. Named ONE BY ONE with the route — never a prefix, never
    /// a whole-file exemption: an unexplained carve-out is how a guard stops being one.
    ///
    /// <para><c>Logging__LogLevel__Default</c> — memex-local applies verbose logging as a
    /// DEPLOYMENT-config override after the helm release ("Applying verbose logging … as a
    /// deployment-config override"), so it arrives as container env directly.</para>
    /// </summary>
    private static readonly HashSet<string> DeliveredOutOfBand =
        new(StringComparer.Ordinal) { "Logging__LogLevel__Default" };

    /// <summary>
    /// 🚨 The GENERAL form of the check above: <b>ANY</b> <c>config.memex_portal</c> key set in a
    /// values file this repo ships must be BOUND in the configmap template.
    ///
    /// <para><b>Why the PreWarm-only version was not enough.</b> That check has existed for a
    /// while and is correct — it is simply scoped to keys starting with <c>PreWarm__</c>, so the
    /// same defect went on shipping under every other prefix. Three instances reached a running
    /// deployment before this test existed:</para>
    /// <list type="number">
    ///   <item><c>Modules__Root</c> (#1924) — set in three AKS values files, rendered by nothing,
    ///     so no module could ever be store-activated and <c>/mcp</c> answered 404.</item>
    ///   <item><c>SelfUpdate__MinRollInterval</c>, <c>WebhookInbox__Targets__0</c>,
    ///     <c>AzureFoundry__Models__3</c> (#1925/#1999) — the restart floor sat inert, and the
    ///     typed one later crashed a portal outright on <c>'' is not a valid TimeSpan</c>.</item>
    ///   <item><c>Modules__Required__N</c> — the AKS overlays blank five entries the image
    ///     requires; un-templated, the override never arrived. On a laptop, where the modules
    ///     cannot be landed instead, the health check held <c>/health</c> at 503 and every
    ///     rollout timed out with the portal never serving.</item>
    /// </list>
    ///
    /// <para>Two things make this test honest rather than noisy, and both were found by running
    /// it:</para>
    /// <list type="bullet">
    ///   <item><b>Read the YAML, never the lines.</b> A line scan sweeps in the <c>secrets:</c>
    ///     section, whose keys are legitimately bound in <c>secrets.yaml</c> under
    ///     <c>.Values.secrets.memex_portal.*</c> — 19 false positives.</item>
    ///   <item><b>Accept the intermediate-variable shape.</b> <c>Portal__BaseUrl</c> is emitted
    ///     via <c>{{- $portalBaseUrl := … }}</c> so it can fall back to the ingress host. Demanding
    ///     the literal one-line binding flags it wrongly; requiring the key to be EMITTED and its
    ///     values path REFERENCED accepts both shapes and still cannot be satisfied by prose.</item>
    /// </list>
    /// </summary>
    [Fact]
    public void EveryConfigKeySetInAValuesFile_IsBoundInTheConfigMap()
    {
        var configMap = ReadPortalConfigMap();

        var missing = ValuesFiles()
            .Where(File.Exists)
            .SelectMany(f => PortalConfigKeysOf(File.ReadAllLines(f))
                .Select(key => (file: Path.GetFileName(f), key)))
            .Distinct()
            .Where(x => !DeliveredOutOfBand.Contains(x.key))
            .Where(x => !IsWiredInConfigMap(configMap, x.key))
            .ToList();

        Assert.True(missing.Count == 0,
            "These config.memex_portal keys are set in a values file but never rendered by "
            + "deploy/helm/templates/memex-portal/config.yaml. The configmap names every key "
            + "explicitly and the Deployment's only env path is envFrom on it, so the setting "
            + "reaches NO container — silently, with helm reporting success: "
            + string.Join(", ", missing.Select(x => $"{x.key} (set in {x.file})"))
            + ". Template each one — and give a TYPED key a real default, because '' is not a "
            + "valid TimeSpan/int and crashes the portal at startup rather than reading as unset.");
    }

    /// <summary>
    /// EVERY tracked file that becomes a values overlay — not only the three the chart is rendered
    /// with directly. <c>values.deploy.example.yaml</c> is copied to the overlay
    /// <c>deploy/aks/scripts/deploy.sh</c> passes, and <c>values.local.yaml</c> to the one
    /// memex-local generates for a new install; both carry <c>config.memex_portal</c> mappings, so
    /// an untemplated key added to either reaches no container in exactly the same way (Copilot
    /// review, #2104).
    /// </summary>
    private static string[] ValuesFiles() =>
    [
        Chart("deploy/helm/values.yaml"),
        Chart("deploy/aks/values.aks.yaml"),
        Chart("deploy/aks/scripts/values.deploy.example.yaml"),
        Local("values.local.defaults.yaml"),
        // The self-registry MODE layer memex-local passes when it serves plugins from a checkout —
        // the one file that still blanks Modules__Required__0..4 (MeshWeaver#2417), so the
        // contiguity and ceiling rules below must see it.
        Local("values.local.self-registry.yaml"),
        Local("values.local.yaml"),
    ];

    /// <summary>The key family whose rendered range this pair of guards holds.</summary>
    private const string RequiredModulePrefix = "Modules__Required__";

    /// <summary>
    /// 🚨 The <c>Modules__Required__N</c> block must render a CONTIGUOUS range from 0, because a
    /// gap is invisible in exactly the way the whole key family already proved it can be.
    ///
    /// <para>The block is written index by index on purpose — a Helm <c>range</c> renders
    /// correctly and is invisible to <see cref="EveryConfigKeySetInAValuesFile_IsBoundInTheConfigMap"/>,
    /// which looks for the literal key. That contract is what makes the list checkable, and it is
    /// also what gives it a CEILING: a hand-written list stops somewhere. Both failure modes of a
    /// hand-written list are silent at deploy time, so both are asserted here:</para>
    /// <list type="number">
    ///   <item><b>A gap.</b> Delete the index-3 block and an overlay's
    ///     <c>Modules__Required__3</c> reaches no container while 4 and 5 still do — the
    ///     deployment then requires a DIFFERENT set than the one it declared, with helm
    ///     reporting success.</item>
    ///   <item><b>The ceiling drifting out of the comment.</b> The comment states the rendered
    ///     range so the next person adding a module reads the number instead of counting blocks;
    ///     this asserts the sentence still matches the template.</item>
    /// </list>
    ///
    /// <para>Both came from Memex#131: the block stopped at 4 while memex-cloud declared index 5
    /// (<c>MeshWeaver.Mcp.dll</c>), so <c>/mcp</c> went on answering 404 with <c>Mcp__BaseUrl</c>
    /// configured a few lines below it — the fifth recurrence of the named-key omission class
    /// (#1925/#1778/#1780/#2203/#2104).</para>
    /// </summary>
    [Fact]
    public void ModulesRequired_IsRenderedAsAContiguousRangeFromZero()
    {
        var configMap = ReadPortalConfigMap();
        var rendered = RenderedRequiredModuleIndices(configMap);

        Assert.True(rendered.Length > 0,
            $"deploy/helm/templates/memex-portal/config.yaml renders NO {RequiredModulePrefix}N "
            + "key at all. The overlays override the image's required-module list by index "
            + "through this block; with it gone every override reaches no container and the "
            + "image's own list silently wins (Memex#131).");

        Assert.True(rendered[0] == 0,
            $"the {RequiredModulePrefix}N block starts at index {rendered[0]}, not 0. "
            + "Configuration binds the array by index, so a list that does not start at 0 cannot "
            + "override the image's first entry.");

        var ceiling = rendered[^1];
        var gaps = Enumerable.Range(0, ceiling + 1).Except(rendered).ToArray();
        Assert.True(gaps.Length == 0,
            $"the {RequiredModulePrefix}N block renders "
            + string.Join(", ", rendered)
            + $" — index {string.Join(", ", gaps)} missing. A gap is silent: an overlay that sets "
            + "the missing index reaches NO container while its neighbours do, so the deployment "
            + "requires a different module set than the one it declares and helm still reports "
            + "success. Render every index from 0 to the ceiling.");

        // The comment must still name the range it renders. This is the only thing standing
        // between "the ceiling is written down where the next person reads it" and a sentence
        // that quietly describes the previous ceiling — which is how Memex#128 came to declare
        // index 5 against a comment that said the chart renders 0..4.
        Assert.True(configMap.Contains($"renders 0..{ceiling}", StringComparison.Ordinal),
            $"the {RequiredModulePrefix}N block renders 0..{ceiling}, but no comment in "
            + "deploy/helm/templates/memex-portal/config.yaml says so. The ceiling is only useful "
            + "written down: the overlay that hits it lives in another repository, and its author "
            + "sees this file, not this test. Say `renders 0..N` in the block's comment.");
    }

    /// <summary>
    /// 🚨 No values file may declare a <c>Modules__Required__N</c> ABOVE the rendered ceiling —
    /// and the failure says what the ceiling is.
    ///
    /// <para><see cref="EveryConfigKeySetInAValuesFile_IsBoundInTheConfigMap"/> already fails on
    /// such a key, but it fails as one of a generic list of "template each one", which is the
    /// wrong instruction for this family: the fix is not a new key somewhere, it is one more
    /// <c>hasKey</c> block on an ordered list whose ceiling the author has to know. Memex#131 is
    /// the whole argument — the person adding index 5 could see the key was refused but not what
    /// the limit was, and the chart is in a different repository from the overlay.</para>
    /// </summary>
    [Fact]
    public void NoValuesFile_DeclaresAModulesRequiredIndexAboveTheCeiling()
    {
        var configMap = ReadPortalConfigMap();
        var rendered = RenderedRequiredModuleIndices(configMap);
        var ceiling = rendered.Length == 0 ? -1 : rendered[^1];

        var over = ValuesFiles()
            .Where(File.Exists)
            .SelectMany(f => PortalConfigKeysOf(File.ReadAllLines(f))
                .Where(k => k.StartsWith(RequiredModulePrefix, StringComparison.Ordinal))
                .Select(k => (file: Path.GetFileName(f), key: k, index: IndexOfKey(k))))
            .Where(x => x.index > ceiling)
            .Distinct()
            .ToList();

        Assert.True(over.Count == 0,
            "These values entries declare a required-module index the chart does not render, so "
            + "they reach NO container and the module is not required at all — silently, with "
            + "helm reporting success: "
            + string.Join(", ", over.Select(x => $"{x.key} (set in {x.file})"))
            + $". deploy/helm/templates/memex-portal/config.yaml renders {RequiredModulePrefix}"
            + $"0..{ceiling} — RAISE THE CEILING by adding one more `hasKey` block per index (a "
            + "Helm range would render correctly and be invisible to the key-literal guards), and "
            + "update the comment that states the rendered range.");
    }

    private static string ReadPortalConfigMap() =>
        File.ReadAllText(Chart("deploy/helm/templates/memex-portal/config.yaml"));

    /// <summary>
    /// The <c>Modules__Required__N</c> indices the configmap actually BINDS, ascending. Candidates
    /// come from executable template text only and each is confirmed through
    /// <see cref="IsWiredInConfigMap"/>, so a key named in a Helm comment — this file has nine
    /// such blocks, and the one above this very family names indices in prose — can never count as
    /// rendered.
    /// </summary>
    private static int[] RenderedRequiredModuleIndices(string configMap) =>
        Regex.Matches(
                ExecutableTemplateOf(configMap),
                $@"^\s{{2}}{Regex.Escape(RequiredModulePrefix)}(\d+):\s",
                RegexOptions.Multiline)
            .Select(m => int.Parse(m.Groups[1].Value, CultureInfo.InvariantCulture))
            .Where(i => IsWiredInConfigMap(configMap, $"{RequiredModulePrefix}{i}"))
            .Distinct()
            .Order()
            .ToArray();

    /// <summary>The numeric suffix of a <c>Modules__Required__N</c> key; -1 when it is not one.
    /// </summary>
    private static int IndexOfKey(string key) =>
        int.TryParse(key[RequiredModulePrefix.Length..], NumberStyles.None,
            CultureInfo.InvariantCulture, out var index)
            ? index
            : -1;

    /// <summary>
    /// The keys under <c>config.memex_portal</c> of a values file — section-scoped, so the
    /// <c>secrets:</c> block (bound in secrets.yaml, not here) never counts. Comments and nested
    /// values are dropped; only mapping keys at the section's own indent are returned.
    /// </summary>
    private static IEnumerable<string> PortalConfigKeysOf(IEnumerable<string> lines)
    {
        var inConfig = false;
        var inPortal = false;
        foreach (var raw in lines)
        {
            if (raw.TrimStart().StartsWith('#') || raw.Trim().Length == 0)
                continue;
            var indent = raw.Length - raw.TrimStart().Length;
            var line = raw.Trim();

            if (indent == 0)
            {
                // A new top-level block ends whichever one we were in — this is what keeps
                // `secrets:` out, and it is the whole reason the scan is section-aware.
                inConfig = line.StartsWith("config:", StringComparison.Ordinal);
                inPortal = false;
                continue;
            }
            if (!inConfig)
                continue;
            if (indent == 2)
            {
                inPortal = line.StartsWith("memex_portal:", StringComparison.Ordinal);
                continue;
            }
            if (!inPortal || indent != 4)
                continue;
            var colon = line.IndexOf(':');
            if (colon <= 0)
                continue;
            var key = line[..colon].Trim();
            if (key.Length > 0)
                yield return key;
        }
    }

    /// <summary>
    /// Whether the configmap actually WIRES <paramref name="key"/>: it emits the key as a
    /// configmap entry AND references its values path somewhere in the template. Both halves are
    /// needed — emitting alone would accept a hard-coded value that ignores the deployment, and
    /// referencing alone would accept a Helm variable computed and never written out. Together
    /// they also accept the intermediate-variable shape, which the stricter one-line form rejects.
    /// </summary>
    private static bool IsWiredInConfigMap(string configMap, string key)
    {
        // 🚨 Judge EXECUTABLE template text only. Nine {{- /* … */}} blocks in that file name keys
        // and their .Values paths in prose; one that writes its example line at the entries' own
        // two-space indent satisfies BOTH halves below while templating nothing — verified by
        // seeding exactly that shape, which this helper catches and the un-stripped form passes.
        // "A check a comment can satisfy is not a check" (Copilot review, #2104).
        var code = ExecutableTemplateOf(configMap);
        return Regex.IsMatch(code, $@"^\s{{2}}{Regex.Escape(key)}:\s", RegexOptions.Multiline)
            && code.Contains($".Values.config.memex_portal.{key}", StringComparison.Ordinal);
    }

    /// <summary>The template with its comments removed: Helm's own <c>{{/* … */}}</c> blocks —
    /// which no '#'-stripping can reach — and YAML <c>#</c> lines.</summary>
    private static string ExecutableTemplateOf(string template)
    {
        var withoutHelmComments = Regex.Replace(
            template, @"\{\{-?\s*/\*.*?\*/\s*-?\}\}", "", RegexOptions.Singleline);
        return string.Join("\n", withoutHelmComments
            .Split('\n')
            .Where(l => !l.TrimStart().StartsWith('#')));
    }

    /// <summary>
    /// 🚨 <b>An armed readiness gate with no sweep behind it is a SILENT LIE.</b>
    ///
    /// <para>The bake gate reads state that only the compiling sweep writes, and the health check
    /// deliberately reports Healthy while the bake is <c>NotStarted</c> so a configuration mistake
    /// can never black-hole a pod. The consequence is that <c>GateReadiness=true</c> with
    /// <c>DynamicTypes=false</c> yields a gate that is registered, permanently green, and protects
    /// NOTHING — which is precisely the failure the gate exists to prevent.</para>
    ///
    /// <para>The portal shouts that combination at Critical on startup, but a log line is a
    /// runtime discovery; this is the compile-time one. It matters most right now, because the
    /// fleet has just switched the sweep OFF: the next person who re-arms a gate "for safety"
    /// without also restoring the sweep gets false confidence, not safety.</para>
    /// </summary>
    [Fact]
    public void NoValuesFileArmsTheBakeGateWithoutTheSweepBehindIt()
    {
        var offenders = new[]
            {
                Chart("deploy/helm/values.yaml"),
                Chart("deploy/aks/values.aks.yaml"),
                Local("values.local.defaults.yaml"),
                Local("values.local.self-registry.yaml"),
            }
            .Where(File.Exists)
            .Select(f => (file: Path.GetFileName(f), lines: File.ReadAllLines(f)
                .Select(l => l.Trim())
                .Where(l => !l.StartsWith('#'))
                .ToList()))
            .Where(x => Setting(x.lines, "PreWarm__GateReadiness") == "true"
                        && Setting(x.lines, "PreWarm__DynamicTypes") != "true")
            .Select(x => x.file)
            .ToList();

        Assert.True(offenders.Count == 0,
            "These values files arm PreWarm__GateReadiness while PreWarm__DynamicTypes is not "
            + "true. The gate can only reach a verdict from the compiling sweep, so with the sweep "
            + "off it reports healthy on every rollout and protects nothing — worse than no gate, "
            + "because the deployment believes it is protected. Either restore the sweep or turn "
            + "the gate off. Offending file(s): " + string.Join(", ", offenders));
    }

    /// <summary>The value of a <c>KEY: "value"</c> line, unquoted, or null when unset.</summary>
    private static string? Setting(IEnumerable<string> lines, string key) =>
        lines.FirstOrDefault(l => l.StartsWith(key + ":", StringComparison.Ordinal))
            ?.Split(':', 2)[1].Trim().Trim('"');


    /// <summary>
    /// Whether the configmap actually BINDS <paramref name="key"/> to its values entry, i.e. emits
    /// <c>&lt;key&gt;: "{{ .Values.config.memex_portal.&lt;key&gt; …}}"</c>. The value binding is the
    /// discriminator on purpose: every key in that file is preceded by a Helm comment block
    /// (<c>{{- /* … */}}</c>) that frequently opens with the key name, so a looser "the key appears
    /// somewhere" test would be satisfied by the prose explaining a key nobody templated — the
    /// exact failure mode this guard exists to catch. The trailing character must be a space or the
    /// closing brace so that one key cannot be matched by another that merely starts with it.
    /// </summary>
    private static bool IsBoundInConfigMap(string configMap, string key)
    {
        var binding = $"{key}: \"{{{{ .Values.config.memex_portal.{key}";
        var at = configMap.IndexOf(binding, StringComparison.Ordinal);
        if (at < 0)
            return false;
        var next = at + binding.Length;
        return next < configMap.Length && (configMap[next] == ' ' || configMap[next] == '}');
    }

    /// <summary>A chart values file (moved here, asserted present).</summary>
    private static string Chart(string relative) => ChartRoots.ChartFile(relative);

    /// <summary>A memex-local overlay — this repository's, absence tolerated (see the class remarks).</summary>
    private static string Local(string fileName) =>
        System.IO.Path.Combine(ChartRoots.DeployRoot(), "deploy", "homebrew", "share", fileName);
}
