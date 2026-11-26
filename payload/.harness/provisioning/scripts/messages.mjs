export const lspCommandMissing = (name) => `lsp: нет команды ${name} в PATH`;
export const lspInstallFailed = (command) => `lsp: установка через ${command} не выполнена`;
export const lspReady = (directory) => `lsp: серверы установлены в ${directory}`;
export const lspDotnetMissing = () => "lsp: не найден .NET SDK: ни DOTNET_ROOT, ни dotnet в PATH, ни стандартные каталоги";
export const lspWaiting = () => "lsp: жду завершения установки другим процессом";
export const lspUsage = () => "lsp-mcp.mjs [--install [--force]]";
