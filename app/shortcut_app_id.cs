// Reads and sets a Windows shortcut's (.lnk) System.AppUserModel.ID property: the AppUserModelID
// that gives AirGrabber its own name and icon on notifications and its own taskbar group.
//
// One copy, shared: install.ps1 compiles it from the freshly installed app folder, and
// app/services/app_identity.py (also for app/services/startup.py) from this file, both through
// Windows PowerShell 5.1's Add-Type, once per PowerShell run. C# 5 (what Add-Type compiles in 5.1).
//
//   [AirGrabber.ShortcutAppId]::Get($lnk)          the ID, or "" when the shortcut has none
//   [AirGrabber.ShortcutAppId]::Set($lnk, $appId)  set it, keeping everything else in the shortcut

using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;

namespace AirGrabber
{
    public static class ShortcutAppId
    {
        // PKEY_AppUserModel_ID = {9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}, 5
        private static readonly Guid AppUserModelFormat = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");
        private const uint AppUserModelIdProperty = 5;
        private static readonly Guid ShellLinkClass = new Guid("00021401-0000-0000-C000-000000000046");
        private const int StgmRead = 0;
        private const int StgmReadWrite = 2;
        private const ushort VtLpwstr = 31;
        private const ushort VtBstr = 8;

        public static string Get(string path)
        {
            object link = CreateLink();
            try
            {
                ((IPersistFile)link).Load(path, StgmRead);
                PropertyKey key = AppIdKey();
                PropVariant value;
                ((IPropertyStore)link).GetValue(ref key, out value);
                try
                {
                    if (value.pointer == IntPtr.Zero)
                    {
                        return "";
                    }
                    if (value.vt == VtLpwstr)
                    {
                        return Marshal.PtrToStringUni(value.pointer) ?? "";
                    }
                    if (value.vt == VtBstr)
                    {
                        return Marshal.PtrToStringBSTR(value.pointer) ?? "";
                    }
                    return "";
                }
                finally
                {
                    PropVariantClear(ref value);
                }
            }
            finally
            {
                Marshal.ReleaseComObject(link);
            }
        }

        public static void Set(string path, string appId)
        {
            if (String.IsNullOrEmpty(appId))
            {
                throw new ArgumentException("An AppUserModelID is required.", "appId");
            }
            object link = CreateLink();
            try
            {
                IPersistFile file = (IPersistFile)link;
                file.Load(path, StgmReadWrite);
                IPropertyStore store = (IPropertyStore)link;
                PropertyKey key = AppIdKey();
                PropVariant value = new PropVariant();
                value.vt = VtLpwstr;
                value.pointer = Marshal.StringToCoTaskMemUni(appId);
                try
                {
                    store.SetValue(ref key, ref value);
                    store.Commit();
                }
                finally
                {
                    PropVariantClear(ref value);  // frees the string
                }
                file.Save(path, true);
            }
            finally
            {
                Marshal.ReleaseComObject(link);
            }
        }

        private static object CreateLink()
        {
            return Activator.CreateInstance(Type.GetTypeFromCLSID(ShellLinkClass, true));
        }

        private static PropertyKey AppIdKey()
        {
            PropertyKey key = new PropertyKey();
            key.formatId = AppUserModelFormat;
            key.propertyId = AppUserModelIdProperty;
            return key;
        }

        [DllImport("ole32.dll")]
        private static extern int PropVariantClear(ref PropVariant value);

        [StructLayout(LayoutKind.Sequential)]
        private struct PropertyKey
        {
            public Guid formatId;
            public uint propertyId;
        }

        // PROPVARIANT: a type tag, three reserved words, then a union two pointers wide
        // (16 bytes on 32-bit, 24 on 64-bit Windows). Only string values are used here.
        [StructLayout(LayoutKind.Sequential)]
        private struct PropVariant
        {
            public ushort vt;
            public ushort reserved1;
            public ushort reserved2;
            public ushort reserved3;
            public IntPtr pointer;
            public IntPtr unused;
        }

        [ComImport]
        [Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")]
        [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        private interface IPropertyStore
        {
            void GetCount(out uint count);
            void GetAt(uint index, out PropertyKey key);
            void GetValue(ref PropertyKey key, out PropVariant value);
            void SetValue(ref PropertyKey key, ref PropVariant value);
            void Commit();
        }
    }
}
