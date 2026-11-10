"use strict";
// Allowlist for RPC methods served by Python plugins (e.g. media.*). The list
// comes from the engine's own plugin registry ("plugins.rpc"); names must look
// like "<prefix>.<action>" and can never shadow a built-in method.
const PLUGIN_METHOD = /^[a-z][a-z0-9_-]*\.[A-Za-z][A-Za-z0-9_]*$/;

function createPluginMethods({ request, builtin }) {
  let methods = null;
  async function load() {
    if (!methods) {
      const listed = await request("plugins.rpc", {});
      methods = new Map((Array.isArray(listed?.methods) ? listed.methods : [])
        .filter((item) => typeof item?.name === "string" && PLUGIN_METHOD.test(item.name) && !builtin.has(item.name))
        .map((item) => [item.name, { operation: item.operation === true, settings: item.settings === true }]));
    }
    return methods;
  }
  async function lookup(method) {
    if (typeof method !== "string" || !PLUGIN_METHOD.test(method) || builtin.has(method)) return null;
    return (await load()).get(method) || null;
  }
  async function list() {
    return [...(await load()).entries()].map(([name, options]) => ({ name, ...options }));
  }
  return { lookup, list };
}

module.exports = { createPluginMethods, PLUGIN_METHOD };
