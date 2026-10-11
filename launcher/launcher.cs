// Windows launcher: runs the bundled Python on localcode.py with the same arguments, console and exit code.
// Compiled with the csc.exe that ships with Windows (C# 5): csc /target:exe /out:localcode.exe launcher.cs
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;

[assembly: AssemblyTitle("localcode launcher")]
[assembly: AssemblyDescription("Starts the bundled Python runtime with localcode.py")]
[assembly: AssemblyProduct("localcode")]
[assembly: AssemblyCopyright("Copyright (c) 2026 swaft")]

static class Launcher
{
    // Quote one argument the way CommandLineToArgvW (and so Python) will read it back.
    static string Quote(string a)
    {
        if (a.Length > 0 && a.IndexOfAny(new[] { ' ', '\t', '\n', '\v', '"' }) < 0) return a;
        var sb = new StringBuilder("\"");
        int slashes = 0;
        foreach (char c in a)
        {
            if (c == '\\') { slashes++; continue; }
            if (c == '"') sb.Append('\\', slashes * 2 + 1).Append('"');
            else sb.Append('\\', slashes).Append(c);
            slashes = 0;
        }
        return sb.Append('\\', slashes * 2).Append('"').ToString();
    }

    static int Main(string[] args)
    {
        string dir = AppDomain.CurrentDomain.BaseDirectory;
        string python = Path.Combine(dir, "runtime", "python.exe");
        string script = Path.Combine(dir, "localcode.py");
        if (!File.Exists(python) || !File.Exists(script))
        {
            Console.Error.WriteLine("localcode: runtime\\python.exe or localcode.py is missing next to " + dir);
            return 127;
        }
        var cmd = new StringBuilder("-I ").Append(Quote(script));
        foreach (string a in args) cmd.Append(' ').Append(Quote(a));
        // Ctrl+C reaches Python through the shared console; keep this process alive until Python has handled it.
        Console.CancelKeyPress += (sender, e) => { e.Cancel = true; };
        var psi = new ProcessStartInfo(python, cmd.ToString()) { UseShellExecute = false };
        using (Process p = Process.Start(psi))
        {
            p.WaitForExit();
            return p.ExitCode;
        }
    }
}
