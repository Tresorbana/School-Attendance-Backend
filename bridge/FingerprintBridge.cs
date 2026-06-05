/*
 * FingerprintBridge.exe  —  AttendAI fingerprint capture bridge (DPFP edition)
 *
 * Uses DigitalPersona DPFP managed SDK (DPFPDevNET.dll + DPFPShrNET.dll) which
 * drives the U.are.U 4500 through its proprietary "Authentication Devices" driver.
 * WBF (Windows Biometric Framework) is NOT used — the U.are.U 4500 is not
 * registered as a WBF biometric unit on this machine.
 *
 * Streams each fingerprint scan to stdout as one JSON line:
 *   {"type":"status","status":"ready"}
 *   {"type":"scan","width":-1,"height":3,"data":"<base64-png>"}
 *   {"type":"error","code":"...","message":"..."}
 *
 * Modes:
 *   --pipe-server : Named-pipe server for scheduled-task / service path
 *   (no args)     : Write JSON directly to stdout (NestJS exe-spawn fallback)
 *
 * Build:
 *   build.bat
 */

using System;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.IO.Pipes;
using System.Threading;
using DPFP;
using DPFP.Capture;

namespace AttendAI.Bridge
{
    static class Program
    {
        #region JSON output helpers

        static readonly object WriteLock = new object();

        static void WriteJson(string json)
        {
            lock (WriteLock)
            {
                Console.WriteLine(json);
                Console.Out.Flush();
            }
        }

        static string Esc(string s)
        {
            if (s == null) return "";
            return s.Replace("\\", "\\\\")
                    .Replace("\"", "\\\"")
                    .Replace("\r", "\\r")
                    .Replace("\n", "\\n");
        }

        static void WriteStatus(string status)
        {
            WriteJson("{\"type\":\"status\",\"status\":\"" + Esc(status) + "\"}");
        }

        static void WriteError(string code, string message)
        {
            WriteJson("{\"type\":\"error\",\"code\":\"" + Esc(code) + "\",\"message\":\"" + Esc(message) + "\"}");
        }

        static void WriteScan(int width, int height, byte[] data)
        {
            string b64 = Convert.ToBase64String(data);
            WriteJson("{\"type\":\"scan\",\"width\":" + width + ",\"height\":" + height + ",\"data\":\"" + b64 + "\"}");
        }

        // Non-JSON debug lines; NestJS logs them as "Bridge non-JSON: ..."
        static void WriteDebug(string msg)
        {
            lock (WriteLock)
            {
                Console.WriteLine("[Bridge] " + msg);
                Console.Out.Flush();
            }
        }

        #endregion

        #region DPFP capture

        class DpfpCapture : DPFP.Capture.EventHandler
        {
            private Capture _cap;
            private volatile bool _running = true;

            /// <summary>
            /// Starts the DPFP capture loop and blocks until the pipe closes or
            /// an unrecoverable error occurs.
            /// </summary>
            public void Run()
            {
                try
                {
                    _cap = new Capture(Priority.Low);
                    _cap.EventHandler = this;
                    _cap.StartCapture();
                    WriteDebug("DPFP capture started (U.are.U 4500)");
                    WriteStatus("ready");

                    // Keep alive — callbacks fire on a DPFP background thread.
                    // Flush every 300 ms to detect a closed pipe early.
                    while (_running)
                    {
                        Thread.Sleep(300);
                        try { Console.Out.Flush(); }
                        catch (IOException) { _running = false; }
                    }
                }
                catch (Exception ex)
                {
                    WriteError("DPFP_INIT_ERROR",
                        "Failed to start fingerprint reader: " + ex.Message +
                        " — ensure the U.are.U 4500 is plugged in and DPFPApi.dll is in System32.");
                }
                finally
                {
                    if (_cap != null) try { _cap.StopCapture(); } catch { }
                }
            }

            // ── EventHandler interface ───────────────────────────────────────

            public void OnComplete(object capture, string serial, Sample sample)
            {
                if (!_running) return;
                try
                {
                    WriteDebug("Finger captured — converting to PNG");
                    var conv = new SampleConversion();
                    Bitmap bmp = null;
                    conv.ConvertToPicture(sample, ref bmp);

                    if (bmp != null)
                    {
                        using (var ms = new MemoryStream())
                        {
                            bmp.Save(ms, ImageFormat.Png);
                            // width=-1, height=3 = PNG comprAlg — same encoding as
                            // WBF intermediate captures so the NestJS gateway handles it.
                            WriteScan(-1, 3, ms.ToArray());
                        }
                        bmp.Dispose();
                    }
                    else
                    {
                        WriteError("DPFP_NO_BITMAP",
                            "SampleConversion.ConvertToPicture returned null — SDK version mismatch?");
                    }
                }
                catch (IOException) { _running = false; }
                catch (Exception ex) { WriteError("DPFP_COMPLETE_ERROR", ex.Message); }
            }

            public void OnFingerGone(object capture, string serial) { }

            public void OnFingerTouch(object capture, string serial)
            {
                WriteDebug("Finger touched sensor");
            }

            public void OnReaderConnect(object capture, string serial)
            {
                WriteDebug("Reader connected: " + serial);
                try { WriteStatus("ready"); }
                catch (IOException) { _running = false; }
            }

            public void OnReaderDisconnect(object capture, string serial)
            {
                WriteDebug("Reader disconnected: " + serial);
                try { WriteStatus("disconnected"); }
                catch (IOException) { _running = false; }
            }

            public void OnSampleQuality(object capture, string serial, CaptureFeedback feedback) { }
        }

        #endregion

        #region Pipe-server mode (runs as scheduled task in interactive session)

        /*
         * RunPipeServer() is launched by the "AttendAI\FingerprintBridge" scheduled
         * task which runs in the interactive user session.  A named mutex prevents
         * a second instance from starting.
         */
        static void RunPipeServer()
        {
            bool createdNew;
            using (var mutex = new Mutex(true, "AttendAIFingerprintBridge", out createdNew))
            {
                if (!createdNew) return; // another instance is already serving

                while (true)
                {
                    NamedPipeServerStream pipe = null;
                    try
                    {
                        pipe = new NamedPipeServerStream(
                            "AttendAIFingerprint",
                            PipeDirection.Out, 1,
                            PipeTransmissionMode.Byte,
                            PipeOptions.None);

                        pipe.WaitForConnection();
                        Console.SetOut(new StreamWriter(pipe) { AutoFlush = true });

                        new DpfpCapture().Run();
                    }
                    catch (IOException) { /* NestJS disconnected — loop to next client */ }
                    catch (Exception ex)
                    {
                        Console.Error.WriteLine("[PipeServer] " + ex.Message);
                    }
                    finally
                    {
                        Console.SetOut(TextWriter.Null);
                        if (pipe != null) try { pipe.Close(); } catch { }
                        Thread.Sleep(500);
                    }
                }
            }
        }

        #endregion

        static void Main(string[] args)
        {
            if (args.Length >= 1 && args[0] == "--pipe-server")
            {
                RunPipeServer();
                return;
            }

            // Direct / exe-fallback mode — DPFP does not require elevation.
            new DpfpCapture().Run();
        }
    }
}
