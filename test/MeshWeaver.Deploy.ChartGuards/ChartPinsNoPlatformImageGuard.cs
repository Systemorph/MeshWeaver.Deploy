using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.RegularExpressions;
using Xunit;

namespace MeshWeaver.Deploy.ChartGuards;

/// <summary>
/// 🚨 Ratchet (c) of the platform's <c>PlatformDeliveryNeverWaitsOnPluginsGuard</c>, over the chart
/// that moved here: never pin an image tag — not in a chart value, not in a template, not in a
/// manifest. The platform image an install runs is chosen by its update policy at roll time; a
/// pinned value freezes it, and every past compatibility break "fixed" by a pin became the next
/// outage.
///
/// <para>The platform repository keeps its own copy of this ratchet over what stayed there
/// (compose files, Deployment records); this one scans the chart's new home. The detector is the
/// platform's, copied verbatim — a test assembly cannot be referenced across repositories — so its
/// control (<see cref="ThePinDetector_FiresOnEachPinShape_AndIsSilentOnFloatingDefaults"/>) moved
/// with it: a detector without its control is a scan that may match nothing.</para>
/// </summary>
public class ChartPinsNoPlatformImageGuard
{
    [Fact]
    public void NoShippedChart_PinsAPlatformImage()
    {
        var root = ChartRoots.DeployRoot();
        var files = ShippedConfigFiles();
        // The denominator: the chart's own values and templates must be among what was read, or a
        // move of the chart would leave this scan passing over nothing.
        Assert.Contains(files, f => f.EndsWith(Path.Combine("helm", "values.yaml"), StringComparison.Ordinal));
        Assert.Contains(files, f => f.EndsWith(Path.Combine("aks", "values.aks.yaml"), StringComparison.Ordinal));

        var problems = files
            .SelectMany(f => PinProblems(Path.GetRelativePath(root, f), File.ReadAllText(f)))
            .ToList();
        Assert.True(problems.Count == 0,
            "a shipped chart value, compose file or Deployment record PINS a platform image tag — "
            + "never pin (the update policy chooses the image at roll time):\n  " + string.Join("\n  ", problems));
    }

    [Fact]
    public void ThePinDetector_FiresOnEachPinShape_AndIsSilentOnFloatingDefaults()
    {
        Assert.Empty(PinProblems("v.yaml", "portal:\n  image: \"ghcr.io/systemorph/memex-portal-ai:latest\"\n"));
        Assert.Empty(PinProblems("v.yaml", "image:\n  portal: \"memex-portal-ai\"\n  tag: \"latest\"\n"));
        Assert.Empty(PinProblems("v.yaml", "hostingOperator:\n  image: \"\"\n"));
        Assert.Empty(PinProblems("r.json", "{ \"pinnedImageTag\": null }"));
        Assert.Empty(PinProblems("v.yaml", "env:\n  Mcp__BaseUrl: \"http://memex-portal-service:8080\"\n"));
        Assert.Empty(PinProblems("v.yaml", "env:\n  Mcp__BaseUrl: \"http://memex-portal:8080\"\n"));

        Assert.NotEmpty(PinProblems("v.yaml", "portal:\n  image: \"ghcr.io/systemorph/memex-portal-ai:3.0.0-ci.9321\"\n"));
        Assert.NotEmpty(PinProblems("v.yaml", "migration:\n  image: \"cr.meshweaver.cloud/memex-migration@sha256:abc\"\n"));
        Assert.NotEmpty(PinProblems("v.yaml", "image:\n  portal: \"memex-portal-ai\"\n  tag: \"3.0.0-ci.9321\"\n"));
        Assert.NotEmpty(PinProblems("r.json", "{ \"pinnedImageTag\": \"3.0.0-ci.8080\" }"));
        Assert.NotEmpty(PinProblems("r.yaml", "content:\n  pinnedImageTag: 3.0.0-ci.8080\n"));
    }

    /// <summary>Pin findings in one shipped file: a platform image carrying a version or digest,
    /// an <c>image.tag</c> beside <c>image.portal</c> that is not <c>latest</c>, or a non-empty
    /// <c>pinnedImageTag</c>.</summary>
    internal static List<string> PinProblems(string relativePath, string text)
    {
        var problems = new List<string>();
        // An image REFERENCE: the repository name as the last path segment, then a tag and/or a
        // digest. `memex-portal:8080` (a host and port in a URL) is not one — a port is all digits.
        foreach (Match m in Regex.Matches(text,
                     @"memex-(?:portal-ai|portal|migration)(?![A-Za-z0-9-])(?<ref>(?::[A-Za-z0-9._-]+)?(?:@sha256:[0-9a-f]+)?)"))
        {
            var reference = m.Groups["ref"].Value;
            if (reference.Length == 0 || reference == ":latest") continue;
            if (Regex.IsMatch(reference, @"^:\d+$")) continue;
            problems.Add($"{relativePath}: `{m.Value}` pins a platform image");
        }
        var imageBlock = Regex.Match(text, @"(?m)^(?<indent>\s*)image:\s*\n(?<body>(?:\k<indent>\s+.*\n?)+)");
        if (imageBlock.Success && Regex.IsMatch(imageBlock.Groups["body"].Value, @"(?m)^\s*portal:"))
        {
            var tag = Regex.Match(imageBlock.Groups["body"].Value, @"(?m)^\s*tag:\s*""?(?<t>[^""\s#]*)");
            if (tag.Success && tag.Groups["t"].Value is { Length: > 0 } t && t != "latest")
                problems.Add($"{relativePath}: `image.tag: {t}` pins the platform image");
        }
        foreach (Match m in Regex.Matches(text, @"""?pinnedImageTag""?\s*:\s*(?<v>""[^""]*""|[^\s,}]+)"))
        {
            var v = m.Groups["v"].Value.Trim('"');
            if (v.Length == 0 || v == "null" || v == "~") continue;
            problems.Add($"{relativePath}: `pinnedImageTag: {v}` pins a Deployment record");
        }
        return problems;
    }

    /// <summary>
    /// Every YAML/JSON file of the chart's new home (<c>deploy/</c> in this repository). As in the
    /// platform copy, only test FIXTURES — recorded cluster state, not configuration — are out of
    /// scope; chart TEMPLATES are shipped configuration and are in scope.
    /// </summary>
    private static List<string> ShippedConfigFiles()
    {
        var deploy = ChartRoots.ChartDirectory("deploy");
        var sep = Path.DirectorySeparatorChar;
        return Directory.EnumerateFiles(deploy, "*.*", SearchOption.AllDirectories)
            .Where(f => f.EndsWith(".yaml", StringComparison.Ordinal) || f.EndsWith(".yml", StringComparison.Ordinal)
                        || f.EndsWith(".json", StringComparison.Ordinal))
            .Where(f => !f.Contains($"{sep}test{sep}fixtures{sep}", StringComparison.Ordinal)
                        && !f.Contains($"{sep}node_modules{sep}", StringComparison.Ordinal))
            .ToList();
    }
}
