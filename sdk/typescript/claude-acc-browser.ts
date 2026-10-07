/**
 * Driver toolsetu przeglądarki z Anthropic SDK (browser_toolset_20260801) na bramce claude-acc.
 *
 * SDK prowadzi pętlę, polityki i zgody, a ten driver wykonuje każde wywołanie w Twoim Chrome albo Brave
 * przez demon claude-acc: ukryte karty bez fokusu, Twoje loginy, jedno "Allow" na start przeglądarki, ta sama
 * bramka domen i ten sam dziennik co MCP `browser` w Claude Code.
 *
 *   import Anthropic from "@anthropic-ai/sdk";
 *   import { ClaudeAccBrowser } from "./claude-acc-browser.ts";
 *
 *   const browser = new ClaudeAccBrowser();
 *   try {
 *     const runner = new Anthropic().beta.messages.toolRunner({
 *       model: "claude-opus-5-5", max_tokens: 4096, tools: [browser],
 *       messages: [{ role: "user", content: "Open example.com and tell me the heading." }],
 *     });
 *     for await (const message of runner) console.log(message);
 *   } finally {
 *     await browser.close();
 *   }
 *
 * Wymaga zainstalowanego claude-acc (`claude-acc browser install`) i `@anthropic-ai/sdk` >= 0.132. Tryb `full`
 * bramki (`claude-acc browser mode full`) włącza javascript_exec i file_upload bez pytania; w trybie guarded podajesz
 * własne `confirm` i `filePolicy`, jak w dokumentacji SDK.
 */
import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";

import {
  BetaAbstractBrowserToolset20260801,
  type BetaBrowserMemberResult,
  type BetaBrowserState,
  type BetaBrowserToolsetOptions,
  type BetaToolsetCallContext,
  ToolError,
} from "@anthropic-ai/sdk/helpers/beta/toolsets";
import type { BetaBrowserMemberInput, BetaBrowserMemberName } from "@anthropic-ai/sdk/resources/beta";

const STATE = process.env.CLAUDE_ACC_STATE ?? path.join(os.homedir(), ".local/share/claude-acc");
const BROWSER_DIR = process.env.CLAUDE_ACC_BROWSER_DIR ?? path.join(STATE, "browser");
const CONFIG = process.env.CLAUDE_ACC_BROWSER_CONFIG ?? path.join(STATE, "browser.json");

type Reply = { ok: boolean; result?: any; state?: any; error?: string };
/** What the daemon answers a member call with: the member's result, the tab report and the tab it ran on. */
type MemberReply = { result?: any; state?: any; tab_id?: string };

class HubError extends Error {
  readonly state: any;

  constructor(message: string, state: any) {
    super(message);
    this.state = state;
  }
}

/** Jedna linia JSON na żądanie przez gniazdo unix demona; pierwszy klient bez demona uruchamia go w tle. */
class HubConnection {
  #socket: net.Socket | undefined;
  #buffer = "";
  #next = 0;
  #pending = new Map<number, (reply: Reply) => void>();
  readonly #owner: string;
  readonly #browser: string | undefined;

  constructor(owner: string, browser: string | undefined) {
    this.#owner = owner;
    this.#browser = browser;
  }

  async #connect(): Promise<net.Socket> {
    const sockPath = path.join(BROWSER_DIR, "hub.sock");
    for (let attempt = 0; attempt < 80; attempt++) {
      try {
        const socket = await new Promise<net.Socket>((resolve, reject) => {
          const s = net.createConnection(sockPath, () => resolve(s));
          s.once("error", reject);
        });
        socket.setEncoding("utf8");
        socket.on("data", (chunk: string) => this.#read(chunk));
        socket.on("close", () => this.#closed(socket));
        socket.write(JSON.stringify({ owner: this.#owner, client: "sdk:typescript", persistent: true, browser: this.#browser }) + "\n");
        return socket;
      } catch {
        if (attempt === 0) {
          const python = path.join(STATE, "python");
          spawn(python, [path.join(STATE, "browser.py"), "serve"], { detached: true, stdio: "ignore", cwd: STATE }).unref();
        }
        await new Promise((r) => setTimeout(r, 100));
      }
    }
    throw new Error(`claude-acc browser daemon did not start: ${path.join(BROWSER_DIR, "hub.log")}`);
  }

  #read(chunk: string): void {
    this.#buffer += chunk;
    let newline: number;
    while ((newline = this.#buffer.indexOf("\n")) >= 0) {
      const line = this.#buffer.slice(0, newline);
      this.#buffer = this.#buffer.slice(newline + 1);
      if (!line.trim()) continue;
      const reply = JSON.parse(line) as Reply & { id: number };
      this.#pending.get(reply.id)?.(reply);
      this.#pending.delete(reply.id);
    }
  }

  #closed(socket: net.Socket): void {
    if (this.#socket !== socket) return;
    this.#socket = undefined;
    for (const resolve of this.#pending.values()) resolve({ ok: false, error: "the claude-acc browser daemon closed: retry" });
    this.#pending.clear();
  }

  async call(op: string, args: unknown): Promise<MemberReply> {
    this.#socket ??= await this.#connect();
    const id = ++this.#next;
    const reply = await new Promise<Reply>((resolve) => {
      this.#pending.set(id, resolve);
      this.#socket!.write(JSON.stringify({ id, op, args: args ?? {} }) + "\n");
    });
    if (!reply.ok) throw new HubError(reply.error ?? "browser daemon error", reply.state);
    return reply.result ?? {};
  }

  close(): void {
    this.#socket?.end();
    this.#socket = undefined;
  }
}

function gatewayMode(): string {
  try {
    return JSON.parse(readFileSync(CONFIG, "utf8")).mode ?? "guarded";
  } catch {
    return "guarded";
  }
}

export type ClaudeAccBrowserOptions = Omit<BetaBrowserToolsetOptions, "browserState"> & {
  /** "chrome" or "brave"; by default the claude-acc choice (`claude-acc browser use`). */
  browser?: "chrome" | "brave";
  /** Overrides the gateway mode for javascript_exec and file_upload (by default from the configuration). */
  full?: boolean;
};

/** The browser toolset on the user's own Chrome or Brave through claude-acc. */
export class ClaudeAccBrowser extends BetaAbstractBrowserToolset20260801 {
  #hub: HubConnection;
  #state: any;

  constructor(options: ClaudeAccBrowserOptions = {}) {
    const { browser, full: fullOption, configs, confirm, ...rest } = options;
    const full = fullOption ?? gatewayMode() === "full";
    const hub = new HubConnection(`sdk:${process.pid}:${Math.random().toString(16).slice(2)}`, browser);
    // demon zbiera konsolę i sieć każdej karty; tryb full otwiera też członki za zgodą
    const enabled = { read_console: { enabled: true }, read_network: { enabled: true } } as Record<string, { enabled: boolean }>;
    if (full) Object.assign(enabled, { file_upload: { enabled: true }, javascript_exec: { enabled: true } });
    const self: { driver?: ClaudeAccBrowser } = {};
    super({
      ...rest,
      configs: { ...enabled, ...configs } as BetaBrowserToolsetOptions["configs"],
      confirm: confirm ?? (full ? () => true : undefined),
      browserState: () => self.driver!.#browserState(),
    });
    self.driver = this;
    this.#hub = hub;
  }

  protected override async execute(
    ctx: BetaToolsetCallContext,
    name: BetaBrowserMemberName,
    input: BetaBrowserMemberInput,
  ): Promise<BetaBrowserMemberResult> {
    let reply: MemberReply;
    try {
      reply = await this.#hub.call(name, input);
    } catch (err) {
      if (err instanceof HubError) {
        this.#state = err.state;
        throw new ToolError(err.message);
      }
      throw err;
    }
    this.#state = reply.state;
    const r = reply.result ?? {};
    switch (r.kind) {
      case "navigate":
        return { url: r.url, status: r.status ?? undefined, title: r.title ?? undefined };
      case "image":
        return { data: r.data, mediaType: r.media_type ?? "image/jpeg" };
      case "text":
        return r.text ?? "";
      case "tab":
        return r.tab;
      case "tabs":
        return r.tabs;
      default:
        return; // czysta akcja: SDK dopisuje własne potwierdzenie (Clicked., Typed.)
    }
  }

  async #browserState(): Promise<BetaBrowserState> {
    let state = this.#state;
    this.#state = undefined;
    if (state == null) state = (await this.#hub.call("state", {})).state;
    const changes = (state?.state_changes ?? []).map((change: any) =>
      change.type === "dialog_dismissed" ? { type: change.type, kind: change.kind, message: change.message } : change,
    );
    return { tabs: state?.tabs ?? [], state_changes: changes.length ? changes : undefined };
  }

  override async close(): Promise<void> {
    await super.close(); // najpierw SDK: żadne wywołanie nie używa już przeglądarki
    try {
      await this.#hub.call("close_all", {});
    } catch {
      // demon już nie działa: nie ma czego zamykać
    }
    this.#hub.close();
  }
}
