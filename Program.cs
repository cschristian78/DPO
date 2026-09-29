using System.Diagnostics;
using System.Runtime.InteropServices;

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

AssignKillOnClose(process);

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

static void AssignKillOnClose(Process process)
{
    IntPtr jobHandle = CreateJobObject(IntPtr.Zero, null);
    var info = new JobObjectExtendedLimitInformation
    {
        BasicLimitInformation = new JobObjectBasicLimitInformation
        {
            LimitFlags = 0x2000
        }
    };
    int length = Marshal.SizeOf<JobObjectExtendedLimitInformation>();
    IntPtr buffer = Marshal.AllocHGlobal(length);
    try
    {
        Marshal.StructureToPtr(info, buffer, false);
        if (!SetInformationJobObject(jobHandle, 9, buffer, (uint)length))
            return;
        AssignProcessToJobObject(jobHandle, process.Handle);
    }
    finally
    {
        Marshal.FreeHGlobal(buffer);
    }
}

[DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
static extern IntPtr CreateJobObject(IntPtr lpJobAttributes, string? lpName);

[DllImport("kernel32.dll")]
static extern bool SetInformationJobObject(IntPtr hJob, int jobObjectInfoClass, IntPtr lpJobObjectInfo, uint cbJobObjectInfoLength);

[DllImport("kernel32.dll", SetLastError = true)]
static extern bool AssignProcessToJobObject(IntPtr hJob, IntPtr hProcess);

[StructLayout(LayoutKind.Sequential)]
struct JobObjectBasicLimitInformation
{
    public long PerProcessUserTimeLimit;
    public long PerJobUserTimeLimit;
    public uint LimitFlags;
    public nuint MinimumWorkingSetSize;
    public nuint MaximumWorkingSetSize;
    public uint ActiveProcessLimit;
    public nuint Affinity;
    public uint PriorityClass;
    public uint SchedulingClass;
}

[StructLayout(LayoutKind.Sequential)]
struct IoCounters
{
    public ulong ReadOperationCount;
    public ulong WriteOperationCount;
    public ulong OtherOperationCount;
    public ulong ReadTransferCount;
    public ulong WriteTransferCount;
    public ulong OtherTransferCount;
}

[StructLayout(LayoutKind.Sequential)]
struct JobObjectExtendedLimitInformation
{
    public JobObjectBasicLimitInformation BasicLimitInformation;
    public IoCounters IoInfo;
    public nuint ProcessMemoryLimit;
    public nuint JobMemoryLimit;
    public nuint PeakProcessMemoryUsed;
    public nuint PeakJobMemoryUsed;
}
