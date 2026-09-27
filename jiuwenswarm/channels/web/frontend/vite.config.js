var __assign = (this && this.__assign) || function () {
    __assign = Object.assign || function(t) {
        for (var s, i = 1, n = arguments.length; i < n; i++) {
            s = arguments[i];
            for (var p in s) if (Object.prototype.hasOwnProperty.call(s, p))
                t[p] = s[p];
        }
        return t;
    };
    return __assign.apply(this, arguments);
};
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import svgr from 'vite-plugin-svgr';
import { spawnSync } from 'child_process';
import { createHash, createHmac, randomUUID, timingSafeEqual } from 'node:crypto';
import https from 'node:https';
import path from 'path';
import fs from 'fs';
import os from 'os';
/**
 * 敏感字段键名判断：与后端 jiuwenswarm.common.utils._KV_SENSITIVE_PATTERN +
 * _NAMED_SENSITIVE_KV_PATTERN 的并集语义保持一致。
 *
 * 后端用通用单词边界 ``(?<![A-Za-z0-9])...(?![A-Za-z0-9])``，可正确匹配连字符/
 * 点号等分隔的键名（``my-api-key`` / ``my.token``）。这里采用与之等价的 token 化
 * 方案：将键名按非字母数字切分，若 token 集合命中敏感词即判定为敏感键。该方案
 * 与后端 ``stream_logger._looks_secret`` 思路一致，天然覆盖各种分隔符，且能排除
 * ``context_window_tokens``（``tokens`` 复数 = 计数，非凭证）。
 */
var SECRET_TOKENS = new Set([
    'token', 'password', 'passwd', 'pwd', 'secret', 'apikey', 'authorization',
    'authorisation', 'credential', 'userid',
]);
// 显式排除的非凭证键名（含敏感子串但语义非凭证）。
var NON_SENSITIVE_KEY_OVERRIDES = new Set(['context_window_tokens', 'context_window_token']);
function looksSecretKey(keyLower) {
    if (!keyLower || NON_SENSITIVE_KEY_OVERRIDES.has(keyLower))
        return false;
    // 按非字母数字切分（与后端 _looks_secret 一致：_ - . / 等都是分隔符）。
    var tokens = new Set(keyLower.split(/[^a-z0-9]+/i).filter(function (t) { return t.length > 0; }));
    if (tokens.size === 0)
        return false;
    // "tokens" 复数 = 计数字段（tokens_used / total_tokens），非凭证，排除。
    if (tokens.has('tokens'))
        return false;
    if (setIntersect(tokens, SECRET_TOKENS))
        return true;
    // api_key / api-key → {api, key}；private_key → {private, key}；
    // access_token → {access, token}（token 已覆盖，但显式列出双 token 防漏）；
    // user_id → {user, id}；refresh_token → token 已覆盖。
    if (tokens.has('api') && tokens.has('key'))
        return true;
    if (tokens.has('private') && tokens.has('key'))
        return true;
    if (tokens.has('user') && tokens.has('id'))
        return true;
    return false;
}
function setIntersect(a, b) {
    // 用 Array.from 规避 Set 直接迭代在某些 TS target 下的 TS2802。
    for (var _i = 0, _a = Array.from(a); _i < _a.length; _i++) {
        var x = _a[_i];
        if (b.has(x))
            return true;
    }
    return false;
}
/**
 * 凭证值形态：即便没有敏感键名上下文，值本身是已知前缀的凭证（OpenAI/Bearer/JWT/
 * GitHub/GitLab token）也要脱敏。与后端 _SENSITIVE_PATTERNS 对齐。
 *
 * Bearer 用后行断言只捕获令牌值本体（不含 "Bearer " 前缀），使指纹与后端
 * _BEARER_SENSITIVE_PATTERN 的 group(2)（token 本体）一致，跨端可关联。
 */
var SENSITIVE_VALUE_PATTERNS = [
    { re: /\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b/g }, // JWT
    { re: /\bsk-[A-Za-z0-9]{8,}\b/g }, // OpenAI 风格
    { re: /\bghp_[A-Za-z0-9]{20,}\b/g }, // GitHub PAT
    { re: /\bglpat-[A-Za-z0-9_-]{20,}\b/g }, // GitLab PAT
    { re: /(?<=\bBearer\s+)[A-Za-z0-9\-._~+/]+=*/gi }, // Authorization Bearer（仅 token 本体）
];
/**
 * 对单个敏感值做带指纹的脱敏：``******(fp:xxxxxxxx)``。
 * 指纹 = SHA256(值) 前 4 字节（8 位 hex），与后端 _fingerprint 算法一致，
 * 同一 key 在前后端两套日志中指纹相同，便于跨端关联排查。不可逆。
 *
 * 若 value 本身已是脱敏产物（``******`` 或 ``******(fp:..)``），原样返回不重算，
 * 与后端 _masked_with_fp 的 _is_already_masked 判断一致——避免对"指纹值"再算
 * 指纹导致跨日志关联失效。
 */
var ALREADY_MASKED_RE = new RegExp('^' + '******'.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '(\\(fp:[0-9a-f]{8}\\))?$');
function isAlreadyMasked(value) {
    return !!value && ALREADY_MASKED_RE.test(value);
}
function maskWithFp(value) {
    if (!value)
        return '******';
    if (isAlreadyMasked(value))
        return value;
    try {
        var fp = createHash('sha256').update(value, 'utf8').digest('hex').slice(0, 8);
        return "******(fp:".concat(fp, ")");
    }
    catch (_a) {
        return '******';
    }
}
/**
 * 对值做形态脱敏：把值中出现的凭证片段（sk-/Bearer/JWT 等）原地替换为带指纹掩码。
 * 用于无敏感键名但值含凭证的场景（如一段日志文本里夹带 sk-xxx）。
 */
function maskValueShapes(value) {
    var out = value;
    for (var _i = 0, SENSITIVE_VALUE_PATTERNS_1 = SENSITIVE_VALUE_PATTERNS; _i < SENSITIVE_VALUE_PATTERNS_1.length; _i++) {
        var re = SENSITIVE_VALUE_PATTERNS_1[_i].re;
        out = out.replace(re, function (m) { return maskWithFp(m); });
    }
    return out;
}
/**
 * 递归脱敏任意结构（对象/数组/字符串）。键名命中敏感词的值整体替换为 ``******(fp:..)``；
 * 字符串值再做形态脱敏兜底。与后端 SensitiveDataFilter 行为对齐。
 */
function maskSensitive(payload) {
    if (payload === null || payload === undefined)
        return payload;
    if (Array.isArray(payload)) {
        return payload.map(function (item) { return maskSensitive(item); });
    }
    if (typeof payload === 'object') {
        var result = {};
        for (var _i = 0, _a = Object.entries(payload); _i < _a.length; _i++) {
            var _b = _a[_i], k = _b[0], v = _b[1];
            if (looksSecretKey(k.toLowerCase())) {
                // 敏感键：整体脱敏（保留指纹）。非字符串值先序列化再算指纹，便于关联。
                var strVal = typeof v === 'string' ? v : safeStringify(v);
                result[k] = maskWithFp(strVal);
            }
            else {
                result[k] = maskSensitive(v);
            }
        }
        return result;
    }
    if (typeof payload === 'string') {
        return maskValueShapes(payload);
    }
    return payload;
}
function safeStringify(v) {
    try {
        return typeof v === 'string' ? v : JSON.stringify(v);
    }
    catch (_a) {
        return String(v);
    }
}
/**
 * file-api 使用的项目根目录，需与后端 get_root_dir() 一致。
 * 优先级：环境变量 > 已存在的用户工作区 ~/.jiuwenswarm > 仓库根。
 */
function resolveProjectRootDir() {
    var envRoot = process.env.JIUWENSWARM_ROOT || process.env.JIUWENSWARM_PROJECT_ROOT;
    if (envRoot) {
        var resolved = path.resolve(envRoot);
        console.log('[file-api] 使用环境变量根目录:', resolved);
        return resolved;
    }
    var home = process.env.USERPROFILE || process.env.HOME || '';
    if (home) {
        // 优先检查多实例环境变量
        var envWorkspace = process.env.JIUWENSWARM_DATA_DIR;
        if (envWorkspace) {
            console.log('[file-api] 使用 JIUWENSWARM_DATA_DIR:', path.resolve(envWorkspace));
            return path.resolve(envWorkspace);
        }
        var userWorkspace = path.join(home, '.jiuwenswarm');
        if (fs.existsSync(userWorkspace)) {
            console.log('[file-api] 使用用户工作区:', path.resolve(userWorkspace));
            return path.resolve(userWorkspace);
        }
    }
    var repoRoot = path.resolve(__dirname, '../../../');
    console.log('[file-api] 使用仓库根目录:', repoRoot);
    return repoRoot;
}
var FILE_CONTENT_ENCODING_ALIASES = {
    utf8: 'utf-8',
    'utf_8': 'utf-8',
    gb2312: 'gb18030',
    gbk: 'gb18030',
    'shift-jis': 'shift_jis',
    sjis: 'shift_jis',
    euc_kr: 'euc-kr',
    latin1: 'iso-8859-1',
};
function normalizeFileContentEncoding(encoding) {
    var _a;
    var key = encoding.trim().toLowerCase();
    return (_a = FILE_CONTENT_ENCODING_ALIASES[key]) !== null && _a !== void 0 ? _a : key;
}
function decodeFileContent(raw, requestedEncoding) {
    var normalizedEncoding = normalizeFileContentEncoding(requestedEncoding || 'utf-8');
    if (normalizedEncoding !== 'auto') {
        return {
            content: new TextDecoder(normalizedEncoding, { fatal: true }).decode(raw),
            encoding: normalizedEncoding,
        };
    }
    var candidates = ['utf-8', 'gb18030', 'big5', 'shift_jis', 'euc-kr', 'iso-8859-1'];
    for (var _i = 0, candidates_1 = candidates; _i < candidates_1.length; _i++) {
        var candidate = candidates_1[_i];
        try {
            return {
                content: new TextDecoder(candidate, { fatal: true }).decode(raw),
                encoding: candidate,
            };
        }
        catch (_a) {
            /* try next encoding */
        }
    }
    throw new Error('Unable to decode file with any known encoding');
}
var DOWNLOAD_CONTENT_TYPES = {
    '.md': 'text/markdown; charset=utf-8',
    '.markdown': 'text/markdown; charset=utf-8',
    '.txt': 'text/plain; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.jsonl': 'application/x-ndjson; charset=utf-8',
    '.csv': 'text/csv; charset=utf-8',
    '.html': 'text/html; charset=utf-8',
    '.htm': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.mjs': 'text/javascript; charset=utf-8',
    '.ts': 'text/plain; charset=utf-8',
    '.tsx': 'text/plain; charset=utf-8',
    '.py': 'text/plain; charset=utf-8',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.webp': 'image/webp',
    '.gif': 'image/gif',
    '.svg': 'image/svg+xml',
    '.bmp': 'image/bmp',
    '.avif': 'image/avif',
    '.pdf': 'application/pdf',
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
};
function downloadContentType(filePath) {
    return DOWNLOAD_CONTENT_TYPES[path.extname(filePath).toLowerCase()] || 'application/octet-stream';
}
function handleFileStreamError(res, error) {
    if (res.headersSent) {
        res.destroy(error);
        return;
    }
    res.statusCode = error.code === 'EACCES' || error.code === 'EPERM' ? 403 : 500;
    res.removeHeader('content-length');
    res.removeHeader('content-disposition');
    res.removeHeader('accept-ranges');
    res.removeHeader('content-range');
    res.setHeader('content-type', 'application/json; charset=utf-8');
    res.end(JSON.stringify({
        error: res.statusCode === 403 ? 'file_access_denied' : 'file_read_failed',
    }));
}
function resolveFileDownloadSecret() {
    var envSecret = process.env.JIUWENSWARM_FILE_DOWNLOAD_SECRET;
    if (envSecret && envSecret.length >= 32)
        return envSecret;
    var workspace = process.env.JIUWENSWARM_WORKSPACE || path.join(process.env.HOME || process.env.USERPROFILE || '', '.jiuwenswarm');
    var secretPath = path.join(workspace, 'config', '.file_download_secret');
    try {
        var secret = fs.readFileSync(secretPath, 'utf8').trim();
        return secret.length >= 32 ? secret : null;
    }
    catch (_a) {
        return null;
    }
}
function validateFileDownloadToken(token) {
    var parts = token.split('.');
    if (parts.length !== 2)
        return null;
    var payloadBase64 = parts[0], signature = parts[1];
    var secret = resolveFileDownloadSecret();
    if (!secret)
        return null;
    var expected = createHmac('sha256', secret).update(payloadBase64).digest('hex');
    var actual = Buffer.from(signature, 'hex');
    var expectedBuffer = Buffer.from(expected, 'hex');
    if (actual.length !== expectedBuffer.length || !timingSafeEqual(actual, expectedBuffer))
        return null;
    try {
        var payload = JSON.parse(Buffer.from(payloadBase64, 'base64url').toString('utf8'));
        // 普通 file download token：携带 path 字段
        if (typeof payload.path === 'string' && payload.path && typeof payload.sid === 'string') {
            return { path: payload.path };
        }
        // skill_content_image token：携带 name + relative_path，需解析为绝对路径
        if (String(payload.purpose || '').trim() === 'skill_content_image') {
            var name_1 = String(payload.name || '').trim();
            var relativePath = String(payload.relative_path || '').trim();
            if (!name_1 || !relativePath)
                return null;
            var rootDir = resolveProjectRootDir();
            var skillDir = path.resolve(rootDir, 'agent', 'workspace', 'skills', name_1);
            var fullPath = path.resolve(skillDir, relativePath);
            // 安全检查：确保路径在 skills 目录下
            var skillsRoot = path.resolve(rootDir, 'agent', 'workspace', 'skills');
            var rel = path.relative(skillsRoot, fullPath);
            if (rel.startsWith('..') || path.isAbsolute(rel))
                return null;
            return { path: fullPath };
        }
        return null;
    }
    catch (_a) {
        return null;
    }
}
/** WS proxy 中常见的、可安全忽略的 socket 错误码（跨平台） */
var WS_PROXY_IGNORABLE_CODES = new Set([
    'EPIPE', // 对端已关闭
    'ECONNRESET', // 连接被重置
    'ECONNABORTED', // 连接被中止 (Windows 常见)
    'ECONNREFUSED', // 后端未启动 / 端口不可达
    'ERR_STREAM_WRITE_AFTER_END',
]);
/** 过滤 Vite 内置的 ws proxy socket 报错，避免控制台刷屏 */
function suppressWsProxySocketErrors() {
    return {
        name: 'suppress-ws-proxy-socket-errors',
        config: function (config) {
            var logger = config.logger;
            if (!(logger === null || logger === void 0 ? void 0 : logger.error))
                return;
            var orig = logger.error.bind(logger);
            logger.error = function (msg, opts) {
                var _a;
                if (typeof msg === 'string' && msg.includes('ws proxy socket error')) {
                    var code = (_a = opts === null || opts === void 0 ? void 0 : opts.error) === null || _a === void 0 ? void 0 : _a.code;
                    if (code && WS_PROXY_IGNORABLE_CODES.has(code))
                        return;
                }
                orig(msg, opts);
            };
        },
    };
}
/** 在 dev 模式下将前端上报的 /ws req/res/event 记录到本地文件 */
function devWsTrafficLogger() {
    return {
        name: 'dev-ws-traffic-logger',
        configureServer: function (server) {
            var projectRootDir = resolveProjectRootDir();
            var agentDir = path.resolve(projectRootDir, 'agent');
            var logDir = path.resolve(agentDir, '.logs');
            var logFile = path.resolve(logDir, 'ws-dev.log');
            fs.mkdirSync(logDir, { recursive: true });
            // 每次前端 dev 服务启动时清空日志，避免历史数据干扰排查。
            fs.writeFileSync(logFile, '', 'utf8');
            server.middlewares.use('/__dev/ws-log', function (req, res) {
                if (req.method === 'GET') {
                    var url = new URL(req.url || '/__dev/ws-log', 'http://localhost');
                    var limitRaw = Number(url.searchParams.get('limit') || '300');
                    var limit_1 = Number.isFinite(limitRaw) ? Math.max(1, Math.min(2000, Math.floor(limitRaw))) : 300;
                    fs.readFile(logFile, 'utf8', function (error, content) {
                        if (error) {
                            var code = error.code;
                            if (code === 'ENOENT') {
                                res.statusCode = 200;
                                res.setHeader('content-type', 'application/json; charset=utf-8');
                                res.end(JSON.stringify({ ok: true, entries: [], count: 0 }));
                                return;
                            }
                            server.config.logger.error("[dev-ws-logger] read failed: ".concat(error.message));
                            res.statusCode = 500;
                            res.setHeader('content-type', 'application/json; charset=utf-8');
                            res.end(JSON.stringify({ ok: false, error: 'read_failed' }));
                            return;
                        }
                        var lines = content
                            .split('\n')
                            .map(function (line) { return line.trim(); })
                            .filter(Boolean)
                            .slice(-limit_1);
                        var entries = lines.map(function (line) {
                            try {
                                return JSON.parse(line);
                            }
                            catch (_a) {
                                return line;
                            }
                        });
                        res.statusCode = 200;
                        res.setHeader('content-type', 'application/json; charset=utf-8');
                        res.end(JSON.stringify({ ok: true, entries: entries, count: entries.length }));
                    });
                    return;
                }
                if (req.method !== 'POST') {
                    res.statusCode = 405;
                    res.setHeader('content-type', 'application/json; charset=utf-8');
                    res.end(JSON.stringify({ ok: false, error: 'method_not_allowed' }));
                    return;
                }
                var raw = '';
                req.on('data', function (chunk) {
                    raw += chunk.toString();
                });
                req.on('end', function () {
                    var now = new Date().toISOString();
                    var payload = raw;
                    if (raw) {
                        try {
                            payload = JSON.parse(raw);
                        }
                        catch (_a) {
                            payload = raw;
                        }
                    }
                    // 写盘前脱敏：前端会把 config.get/config.validate_model 等报文（含
                    // api_key/token/secret）原样上报给 vite dev server，vite 再 appendFile
                    // 写进 ws-dev.log。此处对 payload 递归脱敏，避免 api_key 明文落盘。
                    // 与后端 SensitiveDataFilter 行为/指纹算法一致，便于跨端关联排查。
                    var maskedPayload = maskSensitive(payload);
                    var line = "".concat(JSON.stringify({ ts: now, payload: maskedPayload }), "\n");
                    fs.appendFile(logFile, line, function (error) {
                        if (error) {
                            server.config.logger.error("[dev-ws-logger] write failed: ".concat(error.message));
                            res.statusCode = 500;
                            res.setHeader('content-type', 'application/json; charset=utf-8');
                            res.end(JSON.stringify({ ok: false, error: 'write_failed' }));
                            return;
                        }
                        res.statusCode = 200;
                        res.setHeader('content-type', 'application/json; charset=utf-8');
                        res.end(JSON.stringify({ ok: true }));
                    });
                });
            });
        },
    };
}
var DESIGNER_UPLOAD_MAX_BYTES = 32 * 1024 * 1024;
function parseMultipartFiles(contentType, body) {
    var boundaryMatch = contentType.match(/boundary=(?:"([^"]+)"|([^;]+))/);
    var boundary = ((boundaryMatch && (boundaryMatch[1] || boundaryMatch[2])) || '').trim();
    if (!boundary)
        return [];
    var boundaryBuf = Buffer.from("--".concat(boundary));
    var files = [];
    var start = body.indexOf(boundaryBuf);
    if (start < 0)
        return [];
    start += boundaryBuf.length;
    while (start < body.length) {
        if (body[start] === 45 && body[start + 1] === 45)
            break;
        if (body[start] === 13 && body[start + 1] === 10)
            start += 2;
        var nextBoundary = body.indexOf(boundaryBuf, start);
        if (nextBoundary < 0)
            break;
        var part = body.subarray(start, nextBoundary);
        start = nextBoundary + boundaryBuf.length;
        var headerEnd = part.indexOf('\r\n\r\n');
        if (headerEnd < 0)
            continue;
        var header = part.subarray(0, headerEnd).toString('utf-8');
        var content = part.subarray(headerEnd + 4);
        if (content.length >= 2 && content[content.length - 2] === 13 && content[content.length - 1] === 10) {
            content = content.subarray(0, content.length - 2);
        }
        var nameMatch = header.match(/name="([^"]+)"/);
        var filenameMatch = header.match(/filename="([^"]*)"/);
        if (!nameMatch || nameMatch[1] !== 'file' || !filenameMatch)
            continue;
        var typeMatch = header.match(/Content-Type:\s*([^\r\n]+)/i);
        files.push({
            filename: filenameMatch[1] || 'upload.bin',
            mimeType: (typeMatch && typeMatch[1] && typeMatch[1].trim()) || 'application/octet-stream',
            data: Buffer.from(content),
        });
    }
    return files;
}
/** 将文件读取接口挂到 Vite dev server，避免额外占用 3003 端口 */
function devFileContentApi() {
    var projectRootDir = resolveProjectRootDir();
    var workspaceRootDir = path.resolve(projectRootDir, 'agent');
    var sessionsRootDir = path.resolve(workspaceRootDir, 'sessions');
    var agentTeamsRootDir = path.resolve(projectRootDir, '.agent_teams');
    var webLogsRootDir = path.resolve(workspaceRootDir, '.logs');
    var autoHarnessDir = path.resolve(projectRootDir, 'auto-harness');
    var generateAgentFoldersScriptPath = path.resolve(__dirname, '../../../scripts/generate-agent-folders.js');
    // dev 模式默认开启调试视图，与“前端 dev 即调试模式”一致。
    var wsDisableCompress = true;
    var isMarkdownFile = function (targetPath) {
        var ext = path.extname(targetPath).toLowerCase();
        return ext === '.md' || ext === '.mdx';
    };
    var isPathUnderAllowedRoot = function (targetPath) {
        var relativeWorkspacePath = path.relative(workspaceRootDir, targetPath);
        var inWorkspace = !relativeWorkspacePath.startsWith('..') && !path.isAbsolute(relativeWorkspacePath);
        var relativeAgentTeamsPath = path.relative(agentTeamsRootDir, targetPath);
        var inAgentTeams = !relativeAgentTeamsPath.startsWith('..') && !path.isAbsolute(relativeAgentTeamsPath);
        var relativeLogsPath = path.relative(webLogsRootDir, targetPath);
        var inWebLogs = !relativeLogsPath.startsWith('..') && !path.isAbsolute(relativeLogsPath);
        var relativeAutoHarnessPath = path.relative(autoHarnessDir, targetPath);
        var inAutoHarness = !relativeAutoHarnessPath.startsWith('..') && !path.isAbsolute(relativeAutoHarnessPath);
        return inWorkspace || inAgentTeams || inWebLogs || inAutoHarness;
    };
    return {
        name: 'dev-file-content-api',
        configureServer: function (server) {
            // GitCode API 代理（手动实现，支持 GET/POST）
            server.middlewares.use('/gitcode-api', function (req, res) {
                var proxyPath = (req.url || '').replace(/^\/gitcode-api/, '');
                var chunks = [];
                req.on('data', function (chunk) { return chunks.push(chunk); });
                req.on('end', function () {
                    var body = Buffer.concat(chunks);
                    var proxyReq = https.request({
                        method: req.method,
                        hostname: 'gitcode.com',
                        path: proxyPath,
                        headers: __assign(__assign({}, req.headers), { host: 'gitcode.com' }),
                    }, function (proxyRes) {
                        res.writeHead(proxyRes.statusCode || 200, proxyRes.headers);
                        proxyRes.pipe(res);
                    });
                    proxyReq.on('error', function (err) {
                        console.error('[vite] gitcode-api proxy error:', err.message);
                        res.writeHead(502, { 'content-type': 'application/json' });
                        res.end(JSON.stringify({ error: err.message }));
                    });
                    if (body.length > 0)
                        proxyReq.write(body);
                    proxyReq.end();
                });
            });
            // GitHub OAuth token 兑换代理 → github.com（支持 GET/POST）
            // 用于 POST /login/oauth/access_token（code → access_token）
            server.middlewares.use('/github-oauth', function (req, res) {
                var proxyPath = (req.url || '').replace(/^\/github-oauth/, '');
                var chunks = [];
                req.on('data', function (chunk) { return chunks.push(chunk); });
                req.on('end', function () {
                    var body = Buffer.concat(chunks);
                    var proxyReq = https.request({
                        method: req.method,
                        hostname: 'github.com',
                        path: proxyPath,
                        headers: __assign(__assign({}, req.headers), { host: 'github.com', accept: 'application/json' }),
                    }, function (proxyRes) {
                        res.writeHead(proxyRes.statusCode || 200, proxyRes.headers);
                        proxyRes.pipe(res);
                    });
                    proxyReq.on('error', function (err) {
                        console.error('[vite] github-oauth proxy error:', err.message);
                        res.writeHead(502, { 'content-type': 'application/json' });
                        res.end(JSON.stringify({ error: err.message }));
                    });
                    if (body.length > 0)
                        proxyReq.write(body);
                    proxyReq.end();
                });
            });
            // GitHub API 代理 → api.github.com（支持 GET/POST）
            // 用于 GET /user（access_token → 用户信息）
            server.middlewares.use('/github-api', function (req, res) {
                var proxyPath = (req.url || '').replace(/^\/github-api/, '');
                var chunks = [];
                req.on('data', function (chunk) { return chunks.push(chunk); });
                req.on('end', function () {
                    var body = Buffer.concat(chunks);
                    var proxyReq = https.request({
                        method: req.method,
                        hostname: 'api.github.com',
                        path: proxyPath,
                        headers: __assign(__assign({}, req.headers), { host: 'api.github.com', accept: 'application/json', 'user-agent': 'jiuwenswarm-web' }),
                    }, function (proxyRes) {
                        res.writeHead(proxyRes.statusCode || 200, proxyRes.headers);
                        proxyRes.pipe(res);
                    });
                    proxyReq.on('error', function (err) {
                        console.error('[vite] github-api proxy error:', err.message);
                        res.writeHead(502, { 'content-type': 'application/json' });
                        res.end(JSON.stringify({ error: err.message }));
                    });
                    if (body.length > 0)
                        proxyReq.write(body);
                    proxyReq.end();
                });
            });
            server.middlewares.use('/share-api/snapshot', function (req, res) {
                var writeJson = function (statusCode, payload) {
                    res.statusCode = statusCode;
                    res.setHeader('content-type', 'application/json; charset=utf-8');
                    res.end(JSON.stringify(payload));
                };
                if (req.method !== 'GET' && req.method !== 'HEAD') {
                    writeJson(405, { error: 'method_not_allowed' });
                    return;
                }
                var url = new URL(req.url || '/share-api/snapshot', 'http://localhost');
                var sessionId = (url.searchParams.get('session_id') || '').trim();
                if (!sessionId) {
                    writeJson(400, { error: 'missing_session_id' });
                    return;
                }
                var sessionDir = path.resolve(sessionsRootDir, sessionId);
                var relativeSessionPath = path.relative(sessionsRootDir, sessionDir);
                if (relativeSessionPath.startsWith('..') || path.isAbsolute(relativeSessionPath)) {
                    writeJson(404, { error: 'history_not_found' });
                    return;
                }
                var jsonlHistoryPath = path.resolve(sessionDir, 'history.jsonl');
                var legacyHistoryPath = path.resolve(sessionDir, 'history.json');
                var historyPath = fs.existsSync(jsonlHistoryPath) ? jsonlHistoryPath : legacyHistoryPath;
                if (!fs.existsSync(sessionDir) || !fs.existsSync(historyPath)) {
                    writeJson(404, { error: 'history_not_found' });
                    return;
                }
                try {
                    var historyText = fs.readFileSync(historyPath, 'utf-8');
                    var historyRaw = historyPath.endsWith('.jsonl')
                        ? historyText
                            .split(/\r?\n/)
                            .map(function (line) { return line.trim(); })
                            .filter(Boolean)
                            .map(function (line) { return JSON.parse(line); })
                        : JSON.parse(historyText);
                    if (!Array.isArray(historyRaw)) {
                        writeJson(400, { error: 'invalid_history_shape' });
                        return;
                    }
                    var title = path.basename(sessionDir);
                    var metadataPath = path.resolve(sessionDir, 'metadata.json');
                    if (fs.existsSync(metadataPath)) {
                        var metadata = JSON.parse(fs.readFileSync(metadataPath, 'utf-8'));
                        if (typeof metadata.title === 'string' && metadata.title.trim()) {
                            title = metadata.title.trim();
                        }
                    }
                    if (title === path.basename(sessionDir)) {
                        for (var _i = 0, historyRaw_1 = historyRaw; _i < historyRaw_1.length; _i++) {
                            var record = historyRaw_1[_i];
                            if (!record || typeof record !== 'object')
                                continue;
                            var item = record;
                            if (item.role === 'user' && typeof item.content === 'string' && item.content.trim()) {
                                title = item.content.trim().replace(/\n/g, ' ').slice(0, 80);
                                break;
                            }
                        }
                    }
                    var now = new Date();
                    var filename = "jiuwenswarm-share-".concat(now.toISOString().replace(/[-:]/g, '').replace(/\..+$/, '').replace('T', '-'), ".png");
                    var snapshot = {
                        session_id: sessionId,
                        metadata: {
                            title: title,
                            exported_at: now.toISOString(),
                            filename: filename,
                        },
                        records: historyRaw,
                    };
                    writeJson(200, { filename: filename, snapshot: snapshot });
                }
                catch (error) {
                    writeJson(500, { error: 'snapshot_failed', detail: error.message });
                }
            });
            server.middlewares.use('/file-api/ws-debug-config', function (req, res) {
                if (req.method === 'GET') {
                    res.statusCode = 200;
                    res.setHeader('content-type', 'application/json; charset=utf-8');
                    res.end(JSON.stringify({ wsDisableCompress: wsDisableCompress }));
                    return;
                }
                if (req.method !== 'POST') {
                    res.statusCode = 405;
                    res.setHeader('content-type', 'application/json; charset=utf-8');
                    res.end(JSON.stringify({ error: 'method_not_allowed' }));
                    return;
                }
                var raw = '';
                req.on('data', function (chunk) {
                    raw += chunk.toString();
                });
                req.on('end', function () {
                    try {
                        var payload = raw ? JSON.parse(raw) : {};
                        if (typeof payload.wsDisableCompress !== 'boolean') {
                            res.statusCode = 400;
                            res.setHeader('content-type', 'application/json; charset=utf-8');
                            res.end(JSON.stringify({ error: 'invalid_ws_disable_compress' }));
                            return;
                        }
                        wsDisableCompress = payload.wsDisableCompress;
                        res.statusCode = 200;
                        res.setHeader('content-type', 'application/json; charset=utf-8');
                        res.end(JSON.stringify({ ok: true, wsDisableCompress: wsDisableCompress }));
                    }
                    catch (_a) {
                        res.statusCode = 400;
                        res.setHeader('content-type', 'application/json; charset=utf-8');
                        res.end(JSON.stringify({ error: 'invalid_json' }));
                    }
                });
            });
            server.middlewares.use('/file-api/rebuild-agent-data', function (_req, res) {
                if (_req.method !== 'POST') {
                    res.statusCode = 405;
                    res.setHeader('content-type', 'application/json; charset=utf-8');
                    res.end(JSON.stringify({ error: 'method_not_allowed' }));
                    return;
                }
                try {
                    var runResult = spawnSync(process.execPath, [generateAgentFoldersScriptPath], {
                        encoding: 'utf-8',
                    });
                    if (runResult.status !== 0) {
                        var output = "".concat(runResult.stdout || '', "\n").concat(runResult.stderr || '').trim();
                        res.statusCode = 500;
                        res.setHeader('content-type', 'application/json; charset=utf-8');
                        res.end(JSON.stringify({ error: 'rebuild_failed', detail: output || 'unknown_error' }));
                        return;
                    }
                    res.statusCode = 200;
                    res.setHeader('content-type', 'application/json; charset=utf-8');
                    res.end(JSON.stringify({ ok: true }));
                }
                catch (error) {
                    res.statusCode = 500;
                    res.setHeader('content-type', 'application/json; charset=utf-8');
                    res.end(JSON.stringify({ error: 'rebuild_failed', detail: error.message }));
                }
            });
            server.middlewares.use('/file-api/list-markdown', function (req, res) {
                if (req.method !== 'GET') {
                    res.statusCode = 405;
                    res.setHeader('content-type', 'application/json; charset=utf-8');
                    res.end(JSON.stringify({ error: 'method_not_allowed' }));
                    return;
                }
                var url = new URL(req.url || '/file-api/list-markdown', 'http://localhost');
                var dir = url.searchParams.get('dir');
                if (!dir) {
                    res.statusCode = 400;
                    res.setHeader('content-type', 'application/json; charset=utf-8');
                    res.end(JSON.stringify({ error: 'missing_dir' }));
                    return;
                }
                try {
                    var fullDirPath_1 = path.resolve(projectRootDir, dir);
                    if (!isPathUnderAllowedRoot(fullDirPath_1)) {
                        res.statusCode = 403;
                        res.setHeader('content-type', 'application/json; charset=utf-8');
                        res.end(JSON.stringify({ error: 'forbidden_dir' }));
                        return;
                    }
                    if (!fs.existsSync(fullDirPath_1) || !fs.statSync(fullDirPath_1).isDirectory()) {
                        res.statusCode = 200;
                        res.setHeader('content-type', 'application/json; charset=utf-8');
                        res.end(JSON.stringify({ files: [] }));
                        return;
                    }
                    var files = fs
                        .readdirSync(fullDirPath_1, { withFileTypes: true })
                        .filter(function (entry) { return entry.isFile(); })
                        .map(function (entry) { return entry.name; })
                        .filter(function (name) { return isMarkdownFile(name); })
                        .sort(function (a, b) { return a.localeCompare(b); })
                        .map(function (name) { return ({
                        name: name,
                        path: path.relative(projectRootDir, path.resolve(fullDirPath_1, name)),
                    }); });
                    res.statusCode = 200;
                    res.setHeader('content-type', 'application/json; charset=utf-8');
                    res.end(JSON.stringify({ files: files }));
                }
                catch (error) {
                    res.statusCode = 500;
                    res.setHeader('content-type', 'application/json; charset=utf-8');
                    res.end(JSON.stringify({ error: error.message }));
                }
            });
            server.middlewares.use('/file-api/list-files', function (req, res) {
                if (req.method !== 'GET') {
                    res.statusCode = 405;
                    res.setHeader('content-type', 'application/json; charset=utf-8');
                    res.end(JSON.stringify({ error: 'method_not_allowed' }));
                    return;
                }
                var url = new URL(req.url || '/file-api/list-files', 'http://localhost');
                var dir = url.searchParams.get('dir');
                if (!dir) {
                    res.statusCode = 400;
                    res.setHeader('content-type', 'application/json; charset=utf-8');
                    res.end(JSON.stringify({ error: 'missing_dir' }));
                    return;
                }
                try {
                    var fullDirPath_2 = path.resolve(projectRootDir, dir);
                    if (!isPathUnderAllowedRoot(fullDirPath_2)) {
                        res.statusCode = 403;
                        res.setHeader('content-type', 'application/json; charset=utf-8');
                        res.end(JSON.stringify({ error: 'forbidden_dir' }));
                        return;
                    }
                    if (!fs.existsSync(fullDirPath_2) || !fs.statSync(fullDirPath_2).isDirectory()) {
                        res.statusCode = 200;
                        res.setHeader('content-type', 'application/json; charset=utf-8');
                        res.end(JSON.stringify({ files: [] }));
                        return;
                    }
                    var files = fs
                        .readdirSync(fullDirPath_2, { withFileTypes: true })
                        .sort(function (a, b) {
                        if (a.isDirectory() !== b.isDirectory())
                            return a.isDirectory() ? -1 : 1;
                        return a.name.localeCompare(b.name);
                    })
                        .map(function (entry) {
                        var absolutePath = path.resolve(fullDirPath_2, entry.name);
                        if (entry.isDirectory()) {
                            return {
                                name: entry.name,
                                path: path.relative(projectRootDir, absolutePath),
                                isMarkdown: false,
                                isDirectory: true,
                            };
                        }
                        return {
                            name: entry.name,
                            path: path.relative(projectRootDir, absolutePath),
                            isMarkdown: isMarkdownFile(absolutePath),
                            isDirectory: false,
                        };
                    });
                    res.statusCode = 200;
                    res.setHeader('content-type', 'application/json; charset=utf-8');
                    res.end(JSON.stringify({ files: files }));
                }
                catch (error) {
                    res.statusCode = 500;
                    res.setHeader('content-type', 'application/json; charset=utf-8');
                    res.end(JSON.stringify({ error: error.message }));
                }
            });
            server.middlewares.use('/file-api/download', function (req, res) {
                if (req.method !== 'GET' && req.method !== 'HEAD') {
                    res.statusCode = 405;
                    res.setHeader('content-type', 'application/json; charset=utf-8');
                    res.end(JSON.stringify({ error: 'method_not_allowed' }));
                    return;
                }
                var url = new URL(req.url || '/file-api/download', 'http://localhost');
                var token = url.searchParams.get('token') || '';
                var payload = validateFileDownloadToken(token);
                if (!payload) {
                    res.statusCode = 403;
                    res.setHeader('content-type', 'application/json; charset=utf-8');
                    res.end(JSON.stringify({ error: 'invalid_or_expired_token' }));
                    return;
                }
                var stat;
                try {
                    stat = fs.statSync(payload.path);
                }
                catch (_a) {
                    res.statusCode = 404;
                    res.setHeader('content-type', 'application/json; charset=utf-8');
                    res.end(JSON.stringify({ error: 'file_not_found' }));
                    return;
                }
                if (!stat.isFile()) {
                    res.statusCode = 404;
                    res.setHeader('content-type', 'application/json; charset=utf-8');
                    res.end(JSON.stringify({ error: 'file_not_found' }));
                    return;
                }
                var fileSize = stat.size;
                var start = 0;
                var end = Math.max(0, fileSize - 1);
                var partial = false;
                var range = req.headers.range;
                if (range) {
                    if (!range.startsWith('bytes=') || range.includes(',') || fileSize === 0) {
                        res.statusCode = 416;
                        res.setHeader('content-range', "bytes */".concat(fileSize));
                        res.end();
                        return;
                    }
                    var _b = range.slice(6).split('-', 2), startText = _b[0], endText = _b[1];
                    try {
                        if (startText) {
                            start = Number(startText);
                            end = endText ? Number(endText) : end;
                        }
                        else {
                            var suffixLength = Number(endText);
                            if (!Number.isInteger(suffixLength) || suffixLength <= 0)
                                throw new Error('invalid_suffix');
                            start = Math.max(0, fileSize - suffixLength);
                        }
                        if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || start >= fileSize || end < start)
                            throw new Error('invalid_range');
                        end = Math.min(end, fileSize - 1);
                        partial = true;
                    }
                    catch (_c) {
                        res.statusCode = 416;
                        res.setHeader('content-range', "bytes */".concat(fileSize));
                        res.end();
                        return;
                    }
                }
                var contentLength = fileSize === 0 ? 0 : end - start + 1;
                var inline = ['1', 'true'].includes((url.searchParams.get('inline') || '').toLowerCase());
                var fileName = path.basename(payload.path);
                res.statusCode = partial ? 206 : 200;
                res.setHeader('content-type', downloadContentType(payload.path));
                res.setHeader('content-length', String(contentLength));
                res.setHeader('accept-ranges', 'bytes');
                res.setHeader('content-disposition', "".concat(inline ? 'inline' : 'attachment', "; filename*=UTF-8''").concat(encodeURIComponent(fileName)));
                res.setHeader('cache-control', 'no-store');
                if (partial)
                    res.setHeader('content-range', "bytes ".concat(start, "-").concat(end, "/").concat(fileSize));
                if (req.method === 'HEAD') {
                    res.end();
                    return;
                }
                var fileStream = fs.createReadStream(payload.path, fileSize === 0 ? undefined : { start: start, end: end });
                fileStream.once('error', function (error) {
                    server.config.logger.error("[file-api] Failed to read ".concat(payload.path, ": ").concat(error.message));
                    handleFileStreamError(res, error);
                });
                fileStream.pipe(res);
            });
            server.middlewares.use('/file-api/raw-file', function (req, res) {
                if (req.method !== 'GET' && req.method !== 'HEAD') {
                    res.statusCode = 405;
                    res.setHeader('content-type', 'application/json; charset=utf-8');
                    res.end(JSON.stringify({ error: 'method_not_allowed' }));
                    return;
                }
                var url = new URL(req.url || '/file-api/raw-file', 'http://localhost');
                var filePath = url.searchParams.get('path');
                if (!filePath) {
                    res.statusCode = 400;
                    res.setHeader('content-type', 'application/json; charset=utf-8');
                    res.end(JSON.stringify({ error: '缺少文件路径' }));
                    return;
                }
                try {
                    var fullPath_1 = path.resolve(projectRootDir, filePath);
                    if (!isPathUnderAllowedRoot(fullPath_1)) {
                        res.statusCode = 403;
                        res.setHeader('content-type', 'application/json; charset=utf-8');
                        res.end(JSON.stringify({ error: 'forbidden_path' }));
                        return;
                    }
                    if (!fs.existsSync(fullPath_1) || !fs.statSync(fullPath_1).isFile()) {
                        res.statusCode = 404;
                        res.setHeader('content-type', 'application/json; charset=utf-8');
                        res.end(JSON.stringify({ error: '文件不存在', fullPath: fullPath_1 }));
                        return;
                    }
                    res.statusCode = 200;
                    res.setHeader('content-type', downloadContentType(fullPath_1));
                    res.setHeader('cache-control', 'no-store');
                    if (req.method === 'HEAD') {
                        res.end();
                        return;
                    }
                    var fileStream = fs.createReadStream(fullPath_1);
                    fileStream.once('error', function (error) {
                        server.config.logger.error("[file-api] Failed to read ".concat(fullPath_1, ": ").concat(error.message));
                        handleFileStreamError(res, error);
                    });
                    fileStream.pipe(res);
                }
                catch (error) {
                    res.statusCode = 500;
                    res.setHeader('content-type', 'application/json; charset=utf-8');
                    res.end(JSON.stringify({ error: error.message }));
                }
            });
            // SkillHub API 代理 → teamskills.openjiuwen.com
            // 用于前端直接 POST FormData 发布技能（与 skillhub 架构对齐）
            var hubBaseUrl = process.env.VITE_HUB_API_BASE_URL || 'https://teamskills.openjiuwen.com';
            server.middlewares.use('/hub-api', function (req, res) {
                var proxyPath = (req.url || '').replace(/^\/hub-api/, '');
                var chunks = [];
                req.on('data', function (chunk) { return chunks.push(chunk); });
                req.on('end', function () {
                    var body = Buffer.concat(chunks);
                    var proxyReq = https.request({
                        method: req.method,
                        hostname: new URL(hubBaseUrl).hostname,
                        path: proxyPath,
                        headers: __assign(__assign({}, req.headers), { host: new URL(hubBaseUrl).host }),
                    }, function (proxyRes) {
                        res.writeHead(proxyRes.statusCode || 200, proxyRes.headers);
                        proxyRes.pipe(res);
                    });
                    proxyReq.on('error', function (err) {
                        console.error('[vite] hub-api proxy error:', err.message);
                        res.writeHead(502, { 'content-type': 'application/json' });
                        res.end(JSON.stringify({ error: err.message }));
                    });
                    if (body.length > 0)
                        proxyReq.write(body);
                    proxyReq.end();
                });
            });
            server.middlewares.use('/file-api/upload', function (req, res) {
                if (req.method !== 'POST') {
                    res.statusCode = 405;
                    res.setHeader('content-type', 'application/json; charset=utf-8');
                    res.end(JSON.stringify({ error: 'method_not_allowed', files: [], errors: [{ error: 'method_not_allowed' }] }));
                    return;
                }
                var contentType = req.headers['content-type'] || '';
                var chunks = [];
                req.on('data', function (chunk) { chunks.push(chunk); });
                req.on('end', function () {
                    try {
                        var body = Buffer.concat(chunks);
                        if (body.length > DESIGNER_UPLOAD_MAX_BYTES) {
                            res.statusCode = 413;
                            res.setHeader('content-type', 'application/json; charset=utf-8');
                            res.end(JSON.stringify({ error: 'file_too_large', files: [], errors: [{ error: 'file_too_large' }] }));
                            return;
                        }
                        var parts = contentType.includes('multipart/form-data') ? parseMultipartFiles(contentType, body) : [];
                        if (parts.length === 0) {
                            res.statusCode = 400;
                            res.setHeader('content-type', 'application/json; charset=utf-8');
                            res.end(JSON.stringify({ error: 'missing_file', files: [], errors: [{ error: 'missing_file' }] }));
                            return;
                        }
                        var uploadDir = path.join(workspaceRootDir, 'uploads', 'designer');
                        fs.mkdirSync(uploadDir, { recursive: true });
                        var files = parts.map(function (part) {
                            var safeName = path.basename(part.filename).replace(/[\\/:*?"<>|\u0000-\u001f]/g, '_').slice(0, 120) || 'upload.bin';
                            var storedPath = path.join(uploadDir, "".concat(randomUUID(), "_").concat(safeName));
                            fs.writeFileSync(storedPath, part.data);
                            return {
                                path: storedPath,
                                filename: path.basename(part.filename) || safeName,
                                mime_type: part.mimeType,
                                size: part.data.length,
                            };
                        });
                        res.statusCode = 200;
                        res.setHeader('content-type', 'application/json; charset=utf-8');
                        res.end(JSON.stringify({ ok: true, files: files, errors: [] }));
                    }
                    catch (error) {
                        res.statusCode = 500;
                        res.setHeader('content-type', 'application/json; charset=utf-8');
                        res.end(JSON.stringify({ error: error.message, files: [], errors: [{ error: error.message }] }));
                    }
                });
            });
            // 技能上传：接收 multipart 文件，保存到临时目录，返回文件路径
            // 前端拿到路径后再通过 WebSocket 调用 skills.import_upload / skills.create_from_knowledge
            server.middlewares.use('/file-api/skills/upload-temp', function (req, res) {
                if (req.method !== 'POST') {
                    res.statusCode = 405;
                    res.setHeader('content-type', 'application/json; charset=utf-8');
                    res.end(JSON.stringify({ error: 'method_not_allowed' }));
                    return;
                }
                var contentType = req.headers['content-type'] || '';
                var chunks = [];
                req.on('data', function (chunk) { return chunks.push(chunk); });
                req.on('end', function () {
                    try {
                        var body = Buffer.concat(chunks);
                        var tmpDir = path.join(os.tmpdir(), 'jiuwenswarm_skill_upload');
                        if (!fs.existsSync(tmpDir)) {
                            fs.mkdirSync(tmpDir, { recursive: true });
                        }
                        var fileBuffer = null;
                        var filename = 'upload.zip';
                        if (contentType.includes('multipart/form-data')) {
                            var boundaryMatch = contentType.match(/boundary=(?:"([^"]+)"|([^;]+))/);
                            if (boundaryMatch) {
                                var boundary = boundaryMatch[1] || boundaryMatch[2];
                                var boundaryBuf = Buffer.from("--".concat(boundary));
                                var parts = [];
                                var start = body.indexOf(boundaryBuf) + boundaryBuf.length;
                                while (start < body.length) {
                                    var nextBoundary = body.indexOf(boundaryBuf, start);
                                    if (nextBoundary === -1)
                                        break;
                                    parts.push(body.slice(start, nextBoundary));
                                    start = nextBoundary + boundaryBuf.length;
                                }
                                for (var _i = 0, parts_1 = parts; _i < parts_1.length; _i++) {
                                    var part = parts_1[_i];
                                    var headerEnd = part.indexOf('\r\n\r\n');
                                    if (headerEnd === -1)
                                        continue;
                                    var header = part.slice(0, headerEnd).toString('utf-8');
                                    var content = part.slice(headerEnd + 4, part.length - 2);
                                    var nameMatch = header.match(/name="([^"]+)"/);
                                    var filenameMatch = header.match(/filename="([^"]+)"/);
                                    if (nameMatch && nameMatch[1] === 'file') {
                                        fileBuffer = content;
                                        if (filenameMatch)
                                            filename = filenameMatch[1];
                                    }
                                }
                            }
                        }
                        else {
                            fileBuffer = body;
                            var url = new URL(req.url || '/file-api/skills/upload-temp', 'http://localhost');
                            var nameParam = url.searchParams.get('filename');
                            if (nameParam)
                                filename = nameParam;
                        }
                        if (!fileBuffer) {
                            res.statusCode = 400;
                            res.setHeader('content-type', 'application/json; charset=utf-8');
                            res.end(JSON.stringify({ error: 'no_file_found' }));
                            return;
                        }
                        var safeName = path.basename(filename).replace(/[^a-zA-Z0-9._-]/g, '_');
                        var tempPath = path.join(tmpDir, "".concat(Date.now(), "_").concat(safeName));
                        fs.writeFileSync(tempPath, fileBuffer);
                        res.statusCode = 200;
                        res.setHeader('content-type', 'application/json; charset=utf-8');
                        res.end(JSON.stringify({ path: tempPath }));
                    }
                    catch (error) {
                        res.statusCode = 500;
                        res.setHeader('content-type', 'application/json; charset=utf-8');
                        res.end(JSON.stringify({ error: error.message }));
                    }
                });
            });
            server.middlewares.use('/file-api/file-content', function (req, res) {
                if (req.method === 'GET') {
                    var url = new URL(req.url || '/file-api/file-content', 'http://localhost');
                    var filePath = url.searchParams.get('path');
                    var requestedEncoding = url.searchParams.get('encoding') || 'utf-8';
                    if (!filePath) {
                        res.statusCode = 400;
                        res.setHeader('content-type', 'application/json; charset=utf-8');
                        res.end(JSON.stringify({ error: '缺少文件路径' }));
                        return;
                    }
                    try {
                        var fullPath = path.resolve(projectRootDir, filePath);
                        if (!isPathUnderAllowedRoot(fullPath)) {
                            res.statusCode = 403;
                            res.setHeader('content-type', 'application/json; charset=utf-8');
                            res.end(JSON.stringify({ error: 'forbidden_path' }));
                            return;
                        }
                        if (!fs.existsSync(fullPath)) {
                            if (filePath.replace(/\\/g, '/') === 'agent/workspace/agent-data.json') {
                                try {
                                    var runResult = spawnSync(process.execPath, [generateAgentFoldersScriptPath], {
                                        encoding: 'utf-8',
                                        env: __assign(__assign({}, process.env), { JIUWENSWARM_ROOT: projectRootDir }),
                                        cwd: path.dirname(path.dirname(generateAgentFoldersScriptPath)),
                                    });
                                    if (runResult.status === 0 && fs.existsSync(fullPath)) {
                                        var _a = decodeFileContent(fs.readFileSync(fullPath), requestedEncoding), content_1 = _a.content, encoding_1 = _a.encoding;
                                        res.statusCode = 200;
                                        res.setHeader('content-type', 'text/plain; charset=utf-8');
                                        res.setHeader('X-Original-Encoding', encoding_1);
                                        res.end(content_1);
                                        return;
                                    }
                                }
                                catch (_b) {
                                    /* fall through to 404 */
                                }
                            }
                            res.statusCode = 404;
                            res.setHeader('content-type', 'application/json; charset=utf-8');
                            res.end(JSON.stringify({ error: '文件不存在', fullPath: fullPath }));
                            return;
                        }
                        var _c = decodeFileContent(fs.readFileSync(fullPath), requestedEncoding), content = _c.content, encoding = _c.encoding;
                        res.statusCode = 200;
                        res.setHeader('content-type', 'text/plain; charset=utf-8');
                        res.setHeader('X-Original-Encoding', encoding);
                        res.end(content);
                    }
                    catch (error) {
                        res.statusCode = 500;
                        res.setHeader('content-type', 'application/json; charset=utf-8');
                        res.end(JSON.stringify({ error: error.message }));
                    }
                    return;
                }
                if (req.method !== 'POST') {
                    res.statusCode = 405;
                    res.setHeader('content-type', 'application/json; charset=utf-8');
                    res.end(JSON.stringify({ error: 'method_not_allowed' }));
                    return;
                }
                var raw = '';
                req.on('data', function (chunk) {
                    raw += chunk.toString();
                });
                req.on('end', function () {
                    var payload = {};
                    try {
                        payload = raw ? JSON.parse(raw) : {};
                    }
                    catch (_a) {
                        res.statusCode = 400;
                        res.setHeader('content-type', 'application/json; charset=utf-8');
                        res.end(JSON.stringify({ error: 'invalid_json' }));
                        return;
                    }
                    var requestPath = payload.path;
                    var requestContent = payload.content;
                    if (typeof requestPath !== 'string' || !requestPath.trim()) {
                        res.statusCode = 400;
                        res.setHeader('content-type', 'application/json; charset=utf-8');
                        res.end(JSON.stringify({ error: '缺少文件路径' }));
                        return;
                    }
                    if (typeof requestContent !== 'string') {
                        res.statusCode = 400;
                        res.setHeader('content-type', 'application/json; charset=utf-8');
                        res.end(JSON.stringify({ error: '缺少文件内容' }));
                        return;
                    }
                    var fullPath = path.resolve(projectRootDir, requestPath);
                    if (!isPathUnderAllowedRoot(fullPath)) {
                        res.statusCode = 403;
                        res.setHeader('content-type', 'application/json; charset=utf-8');
                        res.end(JSON.stringify({ error: 'forbidden_path' }));
                        return;
                    }
                    if (!isMarkdownFile(fullPath)) {
                        res.statusCode = 400;
                        res.setHeader('content-type', 'application/json; charset=utf-8');
                        res.end(JSON.stringify({ error: '仅支持保存 Markdown 文件' }));
                        return;
                    }
                    if (!fs.existsSync(fullPath)) {
                        res.statusCode = 404;
                        res.setHeader('content-type', 'application/json; charset=utf-8');
                        res.end(JSON.stringify({ error: '文件不存在' }));
                        return;
                    }
                    fs.writeFile(fullPath, requestContent, 'utf-8', function (error) {
                        if (error) {
                            res.statusCode = 500;
                            res.setHeader('content-type', 'application/json; charset=utf-8');
                            res.end(JSON.stringify({ error: error.message }));
                            return;
                        }
                        res.statusCode = 200;
                        res.setHeader('content-type', 'application/json; charset=utf-8');
                        res.end(JSON.stringify({ ok: true }));
                    });
                });
            });
        },
    };
}
// https://vitejs.dev/config/
function portFromEnv(name, fallback) {
    var _a;
    var value = Number.parseInt((_a = process.env[name]) !== null && _a !== void 0 ? _a : '', 10);
    return Number.isInteger(value) && value > 0 && value <= 65535 ? value : fallback;
}
var frontendPort = portFromEnv('FRONTEND_PORT', 5173);
var webPort = portFromEnv('WEB_PORT', 19000);
var webTarget = "http://127.0.0.1:".concat(webPort);
export default defineConfig({
    plugins: [suppressWsProxySocketErrors(), devWsTrafficLogger(), devFileContentApi(), react(), svgr()],
    optimizeDeps: {
        include: ['exceljs', 'jszip', 'saxes', 'ssf'],
    },
    resolve: {
        dedupe: ['react', 'react-dom'],
        alias: {
            '@': path.resolve(__dirname, './src'),
            'lucide-react': path.resolve(__dirname, './node_modules/lucide-react'),
            react: path.resolve(__dirname, './node_modules/react'),
            'react-dom': path.resolve(__dirname, './node_modules/react-dom'),
        },
    },
    server: {
        host: true,
        allowedHosts: ['127.0.0.1'],
        port: frontendPort,
        strictPort: true,
        proxy: {
            '/api': {
                target: webTarget,
                changeOrigin: true,
            },
            '/skillhub-api': {
                target: 'http://119.8.233.112:8080',
                changeOrigin: true,
                rewrite: function (path) { return path.replace(/^\/skillhub-api/, '/api/v1'); },
            },
            '/ws': {
                target: webTarget,
                ws: true,
                changeOrigin: true,
                configure: function (proxy) {
                    proxy.on('error', function (err, _req, _res) {
                        var code = err.code;
                        if (code && WS_PROXY_IGNORABLE_CODES.has(code)) {
                            return;
                        }
                        console.error('[vite] ws proxy error:', err.message);
                    });
                },
            },
        },
    },
});
