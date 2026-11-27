// Python's RuntimeError and ValueError both surface as a plain Error in the ported code. Some callers catch only RuntimeError
// (a keyboard reply that starts with "ERR", an unavailable store), so those sites mark their errors with kind "RuntimeError".
export const runtimeError = (message) => Object.assign(new Error(message), {kind: "RuntimeError"});
export const isRuntimeError = (error) => error?.kind === "RuntimeError";

// Python's `assert` failures (AssertionError): the checks inside jsc4.reader are asserts, and callers such as the recovery
// verifier catch AssertionError (but not ValueError) to report a malformed image.
export function assertionError(message) {
  return Object.assign(new Error(message), {name: "AssertionError"});
}
