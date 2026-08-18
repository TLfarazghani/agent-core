"use strict";

/* Tool-call parser (JS port of core/parser.py).
 *
 * Single source of truth for extracting tool calls from model output,
 * ported line-for-line from core/parser.py so the WebGPU path shares the
 * exact same grammar as the Windows path. No third-party imports, no eval:
 * the Pythonic subset is tokenized and parsed here.
 *
 * LFM2.5 emits native Pythonic calls, e.g.
 *
 *     <|tool_call_start|>web_search(query="liquid ai lfm")<|tool_call_end|>
 *
 * Arguments are parsed to literals only (strings, numbers, True/False/None,
 * lists, dicts) -- never arbitrary code.
 *
 * Exposes: TOOL_CALL_START, TOOL_CALL_END, ParserError,
 *          extract_blocks, parse_tool_calls, parse_tool_calls_strict,
 *          has_tool_call_blocks.
 *
 * Loads as: CommonJS (`require`), a browser global (`window.AgentParser`),
 * or a classic Web Worker global (`self.AgentParser` via importScripts).
 */

(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.AgentParser = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  const TOOL_CALL_START = "<|tool_call_start|>";
  const TOOL_CALL_END = "<|tool_call_end|>";

  /* Matches core/parser.py: _BLOCK_RE (DOTALL, non-greedy). */
  const _BLOCK_RE = /<\|tool_call_start\|>([\s\S]*?)<\|tool_call_end\|>/g;

  class ParserError extends Error {
    constructor(message) {
      super(message);
      this.name = "ParserError";
    }
  }

  /* Syntax errors (the JS analog of Python's SyntaxError from ast.parse).
   * Only these are skipped by the lenient parser; semantic ParserErrors
   * propagate, exactly like core/parser.py. */
  class ParserSyntaxError extends ParserError {
    constructor(message) {
      super(message);
      this.name = "ParserSyntaxError";
    }
  }

  /* ---------- tokenizer (Pythonic subset) ---------- */

  const _ESCAPES = {
    n: "\n", t: "\t", r: "\r", "\\": "\\", "'": "'", '"': '"', "0": "\0",
    a: "\x07", b: "\b", f: "\f", v: "\v",
  };
  const _STRING_PREFIXES = /^(r|R|u|U|b|B|f|F|rb|Rb|rB|RB|br|Br|bR|BR|rf|rF|Rf|RF|fr|Fr|fR|FR)$/;

  function isDigit(ch) {
    return ch >= "0" && ch <= "9";
  }

  function isIdentStart(ch) {
    return (ch >= "a" && ch <= "z") || (ch >= "A" && ch <= "Z") || ch === "_";
  }

  function isIdentPart(ch) {
    return isIdentStart(ch) || isDigit(ch);
  }

  /* Read a (possibly triple-quoted, possibly prefixed) string literal.
   * Returns { value, end } where end is the index just past the closing quote. */
  function readString(src, i, prefix) {
    const quote = src[i];
    const triple = src[i + 1] === quote && src[i + 2] === quote;
    const bodyStart = i + (triple ? 3 : 1);
    let out = "";
    let p = bodyStart;
    while (p < src.length) {
      const c = src[p];
      if (c === "\\") {
        const nxt = src[p + 1];
        if (nxt === undefined) throw new ParserSyntaxError("unterminated string literal");
        if (prefix.indexOf("r") !== -1 || prefix.indexOf("R") !== -1) {
          out += "\\" + nxt;
          p += 2;
          continue;
        }
        if (_ESCAPES[nxt] !== undefined) {
          out += _ESCAPES[nxt];
          p += 2;
        } else if (nxt === "x") {
          const hex = src.slice(p + 2, p + 4);
          if (!/^[0-9a-fA-F]{2}$/.test(hex)) throw new ParserSyntaxError("malformed \\x escape");
          out += String.fromCharCode(parseInt(hex, 16));
          p += 4;
        } else if (nxt === "u") {
          const hex = src.slice(p + 2, p + 6);
          if (!/^[0-9a-fA-F]{4}$/.test(hex)) throw new ParserSyntaxError("malformed \\u escape");
          out += String.fromCharCode(parseInt(hex, 16));
          p += 6;
        } else {
          /* Unknown escape: keep backslash + char (Python non-raw behavior). */
          out += "\\" + nxt;
          p += 2;
        }
        continue;
      }
      if (triple) {
        if (c === quote && src[p + 1] === quote && src[p + 2] === quote) {
          return { value: out, end: p + 3 };
        }
        out += c;
        p++;
      } else {
        if (c === quote) return { value: out, end: p + 1 };
        if (c === "\n") throw new ParserSyntaxError("unterminated string literal");
        out += c;
        p++;
      }
    }
    throw new ParserSyntaxError("unterminated string literal");
  }

  /* Read an int or float literal (decimal/hex/octal/binary, underscores,
   * exponents). Returns { value, end }. */
  function readNumber(src, i) {
    const two = src.slice(i, i + 2).toLowerCase();
    if (src[i] === "0" && (two[1] === "x" || two[1] === "o" || two[1] === "b")) {
      const base = two[1] === "x" ? 16 : two[1] === "o" ? 8 : 2;
      let j = i + 2;
      const re = base === 16 ? /^[0-9a-fA-F_]+$/ : /^[0-9_]+$/;
      while (j < src.length && re.test(src[j])) j++;
      return { value: parseInt(src.slice(i + 2, j).replace(/_/g, ""), base), end: j };
    }
    let j = i;
    while (j < src.length && (isDigit(src[j]) || src[j] === "_")) j++;
    let isFloat = false;
    if (src[j] === ".") {
      isFloat = true;
      j++;
      while (j < src.length && (isDigit(src[j]) || src[j] === "_")) j++;
    }
    if (src[j] === "e" || src[j] === "E") {
      let k = j + 1;
      if (src[k] === "+" || src[k] === "-") k++;
      if (isDigit(src[k])) {
        isFloat = true;
        j = k;
        while (j < src.length && (isDigit(src[j]) || src[j] === "_")) j++;
      }
    }
    const cleaned = src.slice(i, j).replace(/_/g, "");
    return { value: isFloat ? parseFloat(cleaned) : parseInt(cleaned, 10), end: j };
  }

  function tokenize(src) {
    const tokens = [];
    let i = 0;
    let depth = 0;
    const n = src.length;
    while (i < n) {
      const ch = src[i];
      if (ch === " " || ch === "\t" || ch === "\f" || ch === "\v" || ch === "\r") {
        i++;
      } else if (ch === "\n") {
        if (depth === 0) tokens.push({ type: "NEWLINE", value: "\n" });
        i++;
      } else if (ch === "#") {
        while (i < n && src[i] !== "\n") i++;
      } else if (ch === "'" || ch === '"') {
        const str = readString(src, i, "");
        tokens.push({ type: "STRING", value: str.value });
        i = str.end;
      } else if (isDigit(ch) || (ch === "." && isDigit(src[i + 1]))) {
        const num = readNumber(src, i);
        tokens.push({ type: "NUMBER", value: num.value });
        i = num.end;
      } else if (isIdentStart(ch)) {
        let j = i + 1;
        while (j < n && isIdentPart(src[j])) j++;
        const name = src.slice(i, j);
        if (_STRING_PREFIXES.test(name) && j < n && (src[j] === "'" || src[j] === '"')) {
          const str = readString(src, j, name);
          tokens.push({ type: "STRING", value: str.value });
          i = str.end;
          continue;
        }
        tokens.push({ type: "NAME", value: name });
        i = j;
      } else {
        const two = src.slice(i, i + 2);
        if (two === "**" || two === "//" || two === "==" || two === "!=" ||
            two === "<=" || two === ">=") {
          tokens.push({ type: "OP", value: two });
          i += 2;
          continue;
        }
        if ("()[]{},:=+-*/%<>~@.".indexOf(ch) !== -1) {
          if (ch === "(" || ch === "[" || ch === "{") depth++;
          else if (ch === ")" || ch === "]" || ch === "}") depth--;
          tokens.push({ type: "OP", value: ch });
          i++;
          continue;
        }
        throw new ParserSyntaxError(
          "malformed tool-call block: unexpected character " + JSON.stringify(ch)
        );
      }
    }
    tokens.push({ type: "EOF", value: "" });
    return tokens;
  }

  /* ---------- recursive-descent parser -> AST ---------- */

  /* The AST mirrors CPython's `ast` node names so the walk/parse below can
   * mirror core/parser.py's `ast.walk` + `_parse_call` line-for-line. */

  function parseCallAfterFunc(toks, peek, next, func) {
    next(); /* '(' */
    const args = [];
    const keywords = [];
    while (!(peek().type === "OP" && peek().value === ")")) {
      if (peek().type === "OP" && peek().value === "**") {
        next();
        parseExpr(toks, peek, next);
        keywords.push({ arg: null, value: { type: "Name", id: "**" } });
      } else if (peek().type === "NAME" && peek(1).type === "OP" && peek(1).value === "=") {
        const arg = next().value;
        next(); /* '=' */
        keywords.push({ arg: arg, value: parseExpr(toks, peek, next) });
      } else {
        args.push(parseExpr(toks, peek, next));
      }
      if (peek().type === "OP" && peek().value === ",") next();
      else break;
    }
    next(); /* ')' */
    return { type: "Call", func: func, args: args, keywords: keywords };
  }

  function parseAtom(toks, peek, next) {
    const tok = peek();
    if (tok.type === "NUMBER" || tok.type === "STRING") {
      next();
      return { type: "Constant", value: tok.value };
    }
    if (tok.type === "NAME") {
      if (tok.value === "True") { next(); return { type: "Constant", value: true }; }
      if (tok.value === "False") { next(); return { type: "Constant", value: false }; }
      if (tok.value === "None") { next(); return { type: "Constant", value: null }; }
      next();
      let node = { type: "Name", id: tok.value };
      /* chained attribute access: a.b.c(...) -- Python parses the whole
       * attribute chain as the call target, then funcName() rejects it. */
      while (peek().type === "OP" && peek().value === ".") {
        next();
        const attr = next();
        if (attr.type !== "NAME") throw new ParserSyntaxError("malformed tool-call block");
        node = { type: "Attribute", value: node, attr: attr.value };
      }
      if (peek().type === "OP" && peek().value === "(") {
        return parseCallAfterFunc(toks, peek, next, node);
      }
      return node;
    }
    if (tok.type === "OP" && tok.value === "(") {
      next();
      if (peek().type === "OP" && peek().value === ")") {
        next();
        return { type: "Tuple", elts: [] };
      }
      const first = parseExpr(toks, peek, next);
      if (peek().type === "OP" && peek().value === ",") {
        const elts = [first];
        while (peek().type === "OP" && peek().value === ",") {
          next();
          if (peek().type === "OP" && peek().value === ")") break;
          elts.push(parseExpr(toks, peek, next));
        }
        next(); /* ')' */
        return { type: "Tuple", elts: elts };
      }
      next(); /* ')' */
      return first;
    }
    if (tok.type === "OP" && tok.value === "[") {
      next();
      const elts = [];
      if (peek().type === "OP" && peek().value === "]") {
        next();
        return { type: "List", elts: elts };
      }
      while (true) {
        elts.push(parseExpr(toks, peek, next));
        if (peek().type === "OP" && peek().value === ",") {
          next();
          if (peek().type === "OP" && peek().value === "]") break;
        } else {
          break;
        }
      }
      next(); /* ']' */
      return { type: "List", elts: elts };
    }
    if (tok.type === "OP" && tok.value === "{") {
      next();
      const keys = [];
      const values = [];
      if (peek().type === "OP" && peek().value === "}") {
        next();
        return { type: "Dict", keys: keys, values: values };
      }
      while (true) {
        const key = parseExpr(toks, peek, next);
        if (peek().type === "OP" && peek().value === ":") {
          next();
          keys.push(key);
          values.push(parseExpr(toks, peek, next));
        } else {
          /* Set literals are not supported; matches tools never taking sets. */
          throw new ParserError("non-literal argument: set");
        }
        if (peek().type === "OP" && peek().value === ",") {
          next();
          if (peek().type === "OP" && peek().value === "}") break;
        } else {
          break;
        }
      }
      next(); /* '}' */
      return { type: "Dict", keys: keys, values: values };
    }
    throw new ParserSyntaxError("malformed tool-call block");
  }

  function parseFactor(toks, peek, next) {
    if (peek().type === "OP" && (peek().value === "+" || peek().value === "-" || peek().value === "~")) {
      const op = next().value;
      const unary = op === "+" ? "UAdd" : op === "-" ? "USub" : "Invert";
      return { type: "UnaryOp", op: unary, operand: parseFactor(toks, peek, next) };
    }
    return parsePower(toks, peek, next);
  }

  function parsePower(toks, peek, next) {
    let base = parseAtom(toks, peek, next);
    if (peek().type === "OP" && peek().value === "**") {
      next();
      base = { type: "BinOp", op: "Pow", left: base, right: parseFactor(toks, peek, next) };
    }
    return base;
  }

  function parseTerm(toks, peek, next) {
    let left = parseFactor(toks, peek, next);
    while (peek().type === "OP" && ["*", "/", "//", "%", "@"].indexOf(peek().value) !== -1) {
      const op = next().value;
      left = { type: "BinOp", op: op, left: left, right: parseFactor(toks, peek, next) };
    }
    return left;
  }

  function parseArith(toks, peek, next) {
    let left = parseTerm(toks, peek, next);
    while (peek().type === "OP" && (peek().value === "+" || peek().value === "-")) {
      const op = next().value;
      left = { type: "BinOp", op: op, left: left, right: parseTerm(toks, peek, next) };
    }
    return left;
  }

  function isCompOp(tok) {
    if (tok.type !== "OP" && tok.type !== "NAME") return false;
    return ["==", "!=", "<", ">", "<=", ">=", "in", "is"].indexOf(tok.value) !== -1;
  }

  function parseComparison(toks, peek, next) {
    const left = parseArith(toks, peek, next);
    const ops = [];
    const comparators = [];
    while (isCompOp(peek())) {
      ops.push(next().value);
      comparators.push(parseArith(toks, peek, next));
    }
    if (!ops.length) return left;
    return { type: "Compare", left: left, ops: ops, comparators: comparators };
  }

  function parseNotTest(toks, peek, next) {
    if (peek().type === "NAME" && peek().value === "not") {
      next();
      return { type: "Not", operand: parseNotTest(toks, peek, next) };
    }
    return parseComparison(toks, peek, next);
  }

  function parseAndTest(toks, peek, next) {
    const values = [parseNotTest(toks, peek, next)];
    while (peek().type === "NAME" && peek().value === "and") {
      next();
      values.push(parseNotTest(toks, peek, next));
    }
    return values.length === 1 ? values[0] : { type: "BoolOp", op: "and", values: values };
  }

  function parseOrTest(toks, peek, next) {
    const values = [parseAndTest(toks, peek, next)];
    while (peek().type === "NAME" && peek().value === "or") {
      next();
      values.push(parseAndTest(toks, peek, next));
    }
    return values.length === 1 ? values[0] : { type: "BoolOp", op: "or", values: values };
  }

  function parseExpr(toks, peek, next) {
    return parseOrTest(toks, peek, next);
  }

  /* Parse a whole block into a Module AST. Throws ParserError on syntax
   * errors (mirrors ast.parse raising SyntaxError). */
  function parseModule(src) {
    const toks = tokenize(src);
    let pos = 0;
    const peek = (o) => toks[Math.min(pos + (o || 0), toks.length - 1)];
    const next = () => toks[pos++];
    const body = [];
    while (peek().type !== "EOF") {
      if (peek().type === "NEWLINE") { pos++; continue; }
      if (peek().type === "OP" && peek().value === ";") { pos++; continue; }
      if (peek().type === "NAME" && peek(1).type === "OP" && peek(1).value === "=") {
        const target = { type: "Name", id: next().value };
        next(); /* '=' */
        const value = parseExpr(toks, peek, next);
        body.push({ type: "Assign", targets: [target], value: value });
      } else {
        body.push({ type: "Expr", value: parseExpr(toks, peek, next) });
      }
      if (peek().type === "NEWLINE") {
        pos++;
      } else if (peek().type === "OP" && peek().value === ";") {
        pos++;
      } else if (peek().type !== "EOF") {
        throw new ParserSyntaxError("malformed tool-call block");
      }
    }
    return { type: "Module", body: body };
  }

  /* ---------- AST walk + parse (mirrors core/parser.py) ---------- */

  function childNodes(node) {
    switch (node.type) {
      case "Module": return node.body;
      case "Expr": return [node.value];
      case "Assign": return node.targets.concat([node.value]);
      case "Call":
        return node.args.concat(node.keywords.map((k) => k.value)).concat([node.func]);
      case "keyword": return [node.value];
      case "List": case "Tuple": return node.elts;
      case "Dict": return node.keys.concat(node.values);
      case "UnaryOp": return [node.operand];
      case "BinOp": return [node.left, node.right];
      case "BoolOp": return node.values;
      case "Compare": return [node.left].concat(node.comparators);
      case "Not": return [node.operand];
      case "Attribute": return [node.value];
      default: return [];
    }
  }

  /* ast.walk equivalent (breadth-first). */
  function walk(tree) {
    const queue = [tree];
    const out = [];
    while (queue.length) {
      const node = queue.shift();
      out.push(node);
      for (const child of childNodes(node)) queue.push(child);
    }
    return out;
  }

  function funcName(expr) {
    if (expr.type === "Name") return expr.id;
    throw new ParserError("unsupported call target: " + expr.type);
  }

  /* ast.literal_eval equivalent: only literals, never arbitrary code. */
  function literalEval(node) {
    switch (node.type) {
      case "Constant":
        return node.value;
      case "Tuple":
        return node.elts.map(literalEval);
      case "List":
        return node.elts.map(literalEval);
      case "Dict": {
        const obj = {};
        for (let i = 0; i < node.keys.length; i++) {
          if (node.keys[i] === null) throw new ParserError("non-literal dict key");
          obj[String(literalEval(node.keys[i]))] = literalEval(node.values[i]);
        }
        return obj;
      }
      case "UnaryOp": {
        const v = literalEval(node.operand);
        if (node.op === "USub" && typeof v === "number") return -v;
        if (node.op === "UAdd" && typeof v === "number") return +v;
        throw new ParserError("non-literal argument");
      }
      default:
        throw new ParserError("non-literal argument: " + node.type);
    }
  }

  function parseCallNode(node, index) {
    if (node.args.length) {
      throw new ParserError("positional arguments are not supported; use keyword arguments");
    }
    const args = {};
    for (const kw of node.keywords) {
      if (kw.arg === null) throw new ParserError("unexpected **kwargs in tool call");
      args[kw.arg] = literalEval(kw.value);
    }
    return {
      id: "call_" + String(index + 1).padStart(4, "0"),
      name: funcName(node.func),
      arguments: args,
    };
  }

  /* ---------- public API ---------- */

  function extract_blocks(text) {
    const blocks = [];
    let m;
    _BLOCK_RE.lastIndex = 0;
    while ((m = _BLOCK_RE.exec(text)) !== null) blocks.push(m[1].trim());
    return blocks;
  }

  /* Lenient: blocks that fail to parse as Python syntax are skipped.
   * Mirrors core/parser.py -- ParserError on valid-syntax-but-non-literal
   * calls still propagates. */
  function parse_tool_calls(text) {
    const calls = [];
    let index = 0;
    for (const block of extract_blocks(text)) {
      let tree;
      try {
        tree = parseModule(block);
      } catch (err) {
        if (err instanceof ParserSyntaxError) continue;
        throw err;
      }
      for (const node of walk(tree)) {
        if (node.type === "Call") {
          calls.push(parseCallNode(node, index));
          index++;
        }
      }
    }
    return calls;
  }

  function parse_tool_calls_strict(text) {
    const calls = [];
    for (const block of extract_blocks(text)) {
      let tree;
      try {
        tree = parseModule(block);
      } catch (err) {
        if (err instanceof ParserSyntaxError) throw new ParserError("malformed tool-call block: " + block);
        throw err;
      }
      for (const node of walk(tree)) {
        if (node.type === "Call") {
          calls.push(parseCallNode(node, calls.length));
        }
      }
    }
    return calls;
  }

  function has_tool_call_blocks(text) {
    _BLOCK_RE.lastIndex = 0;
    return _BLOCK_RE.test(text);
  }

  return {
    TOOL_CALL_START: TOOL_CALL_START,
    TOOL_CALL_END: TOOL_CALL_END,
    ParserError: ParserError,
    extract_blocks: extract_blocks,
    parse_tool_calls: parse_tool_calls,
    parse_tool_calls_strict: parse_tool_calls_strict,
    has_tool_call_blocks: has_tool_call_blocks,
  };
});