using System.Diagnostics;

var root = Directory.GetCurrentDirectory();
var venvPython = Path.Combine(root, ".venv", "Scripts", "python.exe");
var fileName = File.Exists(venvPython) ? venvPython : "py";
var arguments = File.Exists(venvPython) ? "app.py" : "-3 app.py";

using var process = new Process
{
    StartInfo = new ProcessStartInfo
    {
        FileName = fileName,
        Arguments = arguments,
        WorkingDirectory = root,
        UseShellExecute = false,
        RedirectStandardOutput = true,
        RedirectStandardError = true,
    }
};

process.OutputDataReceived += (_, e) =>
{
    if (e.Data is not null)
        Console.WriteLine(e.Data);
};
process.ErrorDataReceived += (_, e) =>
{
    if (e.Data is not null)
        Console.Error.WriteLine(e.Data);
};

if (!process.Start())
{
    Console.Error.WriteLine("Could not start Python. Install Python 3 and run from E:\\Development\\DPO.");
    return 1;
}

process.BeginOutputReadLine();
process.BeginErrorReadLine();

Console.CancelKeyPress += (_, e) =>
{
    e.Cancel = true;
    TryStop(process);
};

process.WaitForExit();
return process.ExitCode;

static void TryStop(Process process)
{
    try
    {
        if (!process.HasExited)
            process.Kill(entireProcessTree: true);
    }
    catch (InvalidOperationException)
    {
    }
}
