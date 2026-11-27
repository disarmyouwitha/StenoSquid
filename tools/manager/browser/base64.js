// Strict base64 shape check (the alphabet, correct padding) that stays safe on very large strings. The obvious grouped regex
// /^(?:[A-Za-z0-9+/]{4})*(?:..==|...=)?$/ makes V8 overflow its stack on strings of a few MiB ("Maximum call stack size exceeded"),
// which broke 60 MB backups and repacks; a flat character class has no per-group backtracking state.
export const isBase64 = (text) => typeof text === "string" && text.length % 4 === 0 && /^[A-Za-z0-9+/]*={0,2}$/.test(text);
