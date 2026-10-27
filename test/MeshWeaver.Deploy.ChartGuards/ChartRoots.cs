using System;
using System.IO;
using System.Linq;
using System.Reflection;
using Xunit;

namespace MeshWeaver.Deploy.ChartGuards;

/// <summary>
/// The three roots every chart guard in this project reads from.
///
/// <para><b>The chart is THIS repository's.</b> <c>deploy/helm</c>, <c>deploy/helm-db</c>,
/// <c>deploy/aks/scripts</c>, <c>deploy/aks/manifests</c> and <c>deploy/aks/values.aks.yaml</c>
/// moved out of the platform repository (which must carry no deployment knowledge) into
/// MeshWeaver.Deploy at the same relative paths, and these guards moved with their subject. So a
/// chart file is resolved against this repository's root and its absence FAILS — it never skips: a
/// guard whose subject moved and whose roots did not passes having checked nothing.</para>
///
/// <para><b>The code the chart has to agree with is the PLATFORM's</b> — the portal base image's
/// Dockerfile, the record contract. Those resolve against the platform checkout this assembly was
/// BUILT against (<c>MeshWeaverRoot</c>, baked in as AssemblyMetadata by the project file), so the
/// chart is always compared with the platform it was compiled against.</para>
///
/// <para><b>...and the portal HOSTS'</b> — the probe and drain wiring in
/// <c>src/Memex.Portal.ServiceDefaults</c>, which left the platform for MeshWeaver.Plugins
/// (MeshWeaver#7297, MeshWeaver.Plugins#4333). Those resolve against the MeshWeaver.Plugins checkout
/// this assembly was built against (<c>MeshWeaverPluginsRoot</c>, baked in the same way).</para>
/// </summary>
internal static class ChartRoots
{
    /// <summary>This repository's root: the nearest ancestor of the test binaries that carries the chart.</summary>
    public static string DeployRoot()
    {
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        while (dir is not null && !File.Exists(Path.Combine(dir.FullName, "deploy", "helm", "Chart.yaml")))
            dir = dir.Parent;
        Assert.True(dir is not null,
            "No ancestor of " + AppContext.BaseDirectory + " carries deploy/helm/Chart.yaml. These guards "
            + "run from a MeshWeaver.Deploy checkout; a missing chart is a moved subject, never a skip.");
        return dir!.FullName;
    }

    /// <summary>The platform (MeshWeaver core) checkout this test assembly was built against.</summary>
    public static string PlatformRoot()
    {
        var value = typeof(ChartRoots).Assembly
            .GetCustomAttributes<AssemblyMetadataAttribute>()
            .FirstOrDefault(a => a.Key == "MeshWeaverRoot")?.Value;
        Assert.False(string.IsNullOrWhiteSpace(value),
            "MeshWeaverRoot was not baked into the test assembly (AssemblyMetadata in the csproj).");
        Assert.True(Directory.Exists(value), $"MeshWeaverRoot '{value}' is not a directory.");
        return value!;
    }

    /// <summary>The MeshWeaver.Plugins checkout this test assembly was built against.</summary>
    public static string PluginsRoot()
    {
        var value = typeof(ChartRoots).Assembly
            .GetCustomAttributes<AssemblyMetadataAttribute>()
            .FirstOrDefault(a => a.Key == "MeshWeaverPluginsRoot")?.Value;
        Assert.False(string.IsNullOrWhiteSpace(value),
            "MeshWeaverPluginsRoot was not baked into the test assembly (AssemblyMetadata in the csproj).");
        Assert.True(Directory.Exists(value), $"MeshWeaverPluginsRoot '{value}' is not a directory.");
        return value!;
    }

    /// <summary>
    /// A chart file (repo-relative, forward-slashed) under this repository's root, asserted present:
    /// the chart lives HERE, so a missing file is a moved or deleted subject, never a skip.
    /// </summary>
    public static string ChartFile(string relative)
    {
        var path = Path.Combine(DeployRoot(), relative.Replace('/', Path.DirectorySeparatorChar));
        Assert.True(File.Exists(path),
            $"{relative} is not in this repository ({path}). The deployment chart lives in "
            + "MeshWeaver.Deploy; if it moved again, move these guards with it rather than "
            + "letting them check nothing.");
        return path;
    }

    /// <summary>A chart directory under this repository's root, asserted present (see <see cref="ChartFile"/>).</summary>
    public static string ChartDirectory(string relative)
    {
        var path = Path.Combine(DeployRoot(), relative.Replace('/', Path.DirectorySeparatorChar));
        Assert.True(Directory.Exists(path),
            $"{relative} is not in this repository ({path}). The deployment chart lives in "
            + "MeshWeaver.Deploy; if it moved again, move these guards with it.");
        return path;
    }

    /// <summary>A platform file (repo-relative, forward-slashed) under <see cref="PlatformRoot"/>.</summary>
    public static string PlatformFile(string relative) =>
        Path.Combine(PlatformRoot(), relative.Replace('/', Path.DirectorySeparatorChar));

    /// <summary>
    /// A MeshWeaver.Plugins file (repo-relative, forward-slashed) under <see cref="PluginsRoot"/>,
    /// asserted present: a subject that moved again must fail here, never be read as empty.
    /// </summary>
    public static string PluginsFile(string relative)
    {
        var path = Path.Combine(PluginsRoot(), relative.Replace('/', Path.DirectorySeparatorChar));
        Assert.True(File.Exists(path),
            $"{relative} is not in the MeshWeaver.Plugins checkout ({path}). If it moved again, "
            + "point these guards at where it lives now rather than letting them check nothing.");
        return path;
    }
}
