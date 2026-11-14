# Local PowerShell + PowerCLI copy (optional)

The `core-pwsh` image build looks here before it downloads anything:

- `powershell-<version>-linux-<arm64|x64>.tar.gz` is used instead of the GitHub download
  when the name matches `PWSH_VERSION` and the image CPU.
- `Modules/VMware.PowerCLI` (and the modules it needs) is copied in instead of the
  PowerShell Gallery install. When `POWERCLI_VERSION` is set, that version must be here.

Fill it with `scripts/Save-PwshVendor.ps1`. Everything in this folder except this README is
ignored by git. Leave the folder empty to download as before.
