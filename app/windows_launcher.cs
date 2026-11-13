using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

internal static class Launcher
{
    [STAThread]
    private static void Main()
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        using (var window = new FinanceWindow(AppDomain.CurrentDomain.BaseDirectory))
            Application.Run(window);
    }
}

internal sealed class FinanceWindow : Form
{
    private const string AppTitle = "快递财务智能体";
    private const string ReadyPrefix = "WEB_URL=";
    private const string InstallerPath = @"runtime\MicrosoftEdgeWebView2RuntimeInstallerX64.exe";
    private readonly string root;
    private readonly Label status;
    private readonly WebView2 browser;
    private readonly TaskCompletionSource<Uri> ready = new TaskCompletionSource<Uri>();
    private readonly Queue<string> errors = new Queue<string>();
    private Process child;
    private Uri address;
    private CoreWebView2Environment environment;
    private volatile bool closing;

    internal FinanceWindow(string root)
    {
        this.root = root;
        Text = AppTitle;
        StartPosition = FormStartPosition.CenterScreen;
        Rectangle screen = Screen.PrimaryScreen.WorkingArea;
        ClientSize = new Size(Math.Min(1440, screen.Width - 56), Math.Min(900, screen.Height - 70));
        MinimumSize = new Size(760, 540);
        BackColor = SystemColors.Window;
        browser = new WebView2 { Dock = DockStyle.Fill, Visible = false };
        status = new Label
        {
            Dock = DockStyle.Fill,
            TextAlign = ContentAlignment.MiddleCenter,
            Text = "正在启动本机服务…",
            Padding = new Padding(24),
            Font = new Font(SystemFonts.MessageBoxFont.FontFamily, 11)
        };
        Controls.Add(browser);
        Controls.Add(status);
        Shown += StartServices;
    }

    private async void StartServices(object sender, EventArgs e)
    {
        try
        {
            CoreWebView2Environment.GetAvailableBrowserVersionString();

            StartChild();
            Process running = child;
            var exited = Task.Run(() => { running.WaitForExit(); return running.ExitCode; });
            var deadline = Task.Delay(TimeSpan.FromMinutes(3));
            var startup = await Task.WhenAny(ready.Task, exited, deadline);
            if (closing) return;
            if (startup == deadline) throw new InvalidOperationException("本机服务启动超时。");
            if (startup == exited) throw ChildFailure(await exited);
            address = await ready.Task;

            status.Text = "正在打开网页…";
            environment = await CoreWebView2Environment.CreateAsync(null, ProfileDirectory());
            if (closing) return;
            browser.Visible = true;
            await browser.EnsureCoreWebView2Async(environment);
            if (closing) return;
            ConfigureBrowser(browser, this);
            status.Visible = false;
            browser.CoreWebView2.Navigate(address.AbsoluteUri);

            int exitCode = await exited;
            if (!closing) throw ChildFailure(exitCode);
        }
        catch (WebView2RuntimeNotFoundException)
        {
            ReportFailure("缺少 Microsoft Edge WebView2 运行时。请运行文件夹里的 " + InstallerPath + "，安装后重新打开程序。");
        }
        catch (Exception error)
        {
            ReportFailure(error.GetBaseException().Message);
        }
    }

    private void StartChild()
    {
        string python = Path.Combine(root, "runtime", "python.exe");
        string script = Path.Combine(root, "run_offline.py");
        if (!File.Exists(python) || !File.Exists(script))
            throw new FileNotFoundException("启动文件不完整。请把 EXE 放在完整项目文件夹中再运行。");

        var start = new ProcessStartInfo(python, "-X utf8 -B \"" + script + "\" --managed-window")
        {
            WorkingDirectory = root,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8
        };
        var running = new Process { StartInfo = start };
        running.OutputDataReceived += ReadStatus;
        running.ErrorDataReceived += ReadError;
        if (!running.Start())
        {
            running.Dispose();
            throw new InvalidOperationException("无法启动本机服务。");
        }
        child = running;
        running.BeginOutputReadLine();
        running.BeginErrorReadLine();
    }

    private void ReadStatus(object sender, DataReceivedEventArgs e)
    {
        if (String.IsNullOrWhiteSpace(e.Data) || closing) return;
        if (e.Data.StartsWith(ReadyPrefix, StringComparison.Ordinal))
        {
            Uri target;
            string value = e.Data.Substring(ReadyPrefix.Length);
            if (Uri.TryCreate(value, UriKind.Absolute, out target) && target.Scheme == Uri.UriSchemeHttp &&
                target.Host == "127.0.0.1" && target.Port > 0 && target.AbsolutePath == "/" &&
                target.UserInfo.Length == 0 && target.Query.Length == 0 && target.Fragment.Length == 0)
                ready.TrySetResult(target);
            else
                ready.TrySetException(new InvalidDataException("本机服务返回的网页地址无效。"));
            return;
        }
        try
        {
            BeginInvoke((Action)(() => { if (!closing && address == null) status.Text = e.Data; }));
        }
        catch (InvalidOperationException) { }
    }

    private void ReadError(object sender, DataReceivedEventArgs e)
    {
        if (String.IsNullOrWhiteSpace(e.Data)) return;
        lock (errors)
        {
            if (errors.Count == 5) errors.Dequeue();
            errors.Enqueue(e.Data.Length <= 400 ? e.Data : e.Data.Substring(0, 400));
        }
    }

    private Exception ChildFailure(int exitCode)
    {
        string details;
        lock (errors) details = String.Join(Environment.NewLine, errors);
        return new InvalidOperationException(details.Length > 0 ? details : "本机服务已退出（代码 " + exitCode + "）。");
    }

    private string ProfileDirectory()
    {
        using (var sha = SHA256.Create())
        {
            byte[] path = Encoding.UTF8.GetBytes(Path.GetFullPath(root).TrimEnd('\\').ToUpperInvariant());
            string key = BitConverter.ToString(sha.ComputeHash(path), 0, 12).Replace("-", "");
            return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "CourierFinanceAgent", "WebView2", key);
        }
    }

    private bool IsLocalNavigation(string url)
    {
        if (url == "about:blank") return true;
        if (url.StartsWith("blob:", StringComparison.Ordinal)) url = url.Substring(5);
        Uri target;
        return Uri.TryCreate(url, UriKind.Absolute, out target) && target.Scheme == address.Scheme &&
            target.Host == address.Host && target.Port == address.Port && target.UserInfo.Length == 0;
    }

    private void ConfigureBrowser(WebView2 view, Form window)
    {
        var core = view.CoreWebView2;
        core.NavigationStarting += (sender, e) => { if (!IsLocalNavigation(e.Uri)) e.Cancel = true; };
        core.NewWindowRequested += (sender, e) =>
        {
            e.Handled = true;
            if (closing || !IsLocalNavigation(e.Uri)) return;
            string uri = e.Uri;
            BeginInvoke((Action)(() => OpenPopup(uri)));
        };
        if (window != this) core.WindowCloseRequested += (sender, e) => window.Close();
    }

    private async void OpenPopup(string uri)
    {
        if (closing) return;
        var popup = new Form
        {
            Text = AppTitle + " · 单据",
            StartPosition = FormStartPosition.CenterParent,
            Size = new Size(Math.Min(1050, Width), Math.Min(770, Height))
        };
        try
        {
            var view = new WebView2 { Dock = DockStyle.Fill };
            popup.Controls.Add(view);
            popup.Show(this);
            await view.EnsureCoreWebView2Async(environment);
            if (closing || popup.IsDisposed)
            {
                popup.Dispose();
                return;
            }
            ConfigureBrowser(view, popup);
            view.CoreWebView2.Navigate(uri);
        }
        catch (Exception)
        {
            bool cancelled = closing || popup.IsDisposed;
            popup.Dispose();
            if (!cancelled) MessageBox.Show(this, "无法打开单据窗口，请重试。", AppTitle,
                MessageBoxButtons.OK, MessageBoxIcon.Warning);
        }
    }

    protected override bool ProcessCmdKey(ref Message message, Keys keyData)
    {
        if ((keyData == Keys.F5 || keyData == (Keys.Control | Keys.R)) && browser.CoreWebView2 != null)
        {
            browser.CoreWebView2.Reload();
            return true;
        }
        return base.ProcessCmdKey(ref message, keyData);
    }

    private void ReportFailure(string error)
    {
        if (closing) return;
        StopChild();
        MessageBox.Show(this, error, AppTitle, MessageBoxButtons.OK, MessageBoxIcon.Error);
        Close();
    }

    protected override void OnFormClosing(FormClosingEventArgs e)
    {
        base.OnFormClosing(e);
        if (e.Cancel) return;
        closing = true;
        try
        {
            foreach (Form popup in OwnedForms) popup.Dispose();
            browser.Dispose();
        }
        finally { StopChild(); }
    }

    private void StopChild()
    {
        Process running = child;
        child = null;
        if (running == null) return;
        try { running.StandardInput.Close(); }
        catch (IOException) { }
        if (!running.WaitForExit(5000))
        {
            try { running.Kill(); }
            catch (InvalidOperationException) { }
            running.WaitForExit(2500);
        }
        running.Dispose();
    }
}
