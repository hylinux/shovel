import type { RuntimeConfigField, RuntimeConfigValue } from "./api";

export type ConfigurationDraft = Record<string, string>;
export type SecretAction = "keep" | "replace" | "clear";
export type SecretActions = Record<string, SecretAction>;

export function configurationDraft(fields: RuntimeConfigField[]): ConfigurationDraft {
  return Object.fromEntries(fields.filter((field) => !field.readonly && !field.secret)
    .map((field) => [field.key, field.value === null ? "" : String(field.value)]));
}

export function configurationChanges(fields: RuntimeConfigField[], draft: ConfigurationDraft, secrets: SecretActions): Record<string, RuntimeConfigValue> {
  const changes: Record<string, RuntimeConfigValue> = {};
  for (const field of fields) {
    if (field.readonly) continue;
    if (field.secret) {
      if (secrets[field.key] === "clear") changes[field.key] = "";
      else if (secrets[field.key] === "replace") {
        if (!draft[field.key]) throw new Error("Enter a replacement key or choose Clear saved key.");
        changes[field.key] = draft[field.key];
      }
      continue;
    }
    const text = draft[field.key] ?? "";
    let value: RuntimeConfigValue = text;
    if (field.type === "boolean") value = text === "true";
    else if (field.type === "number") {
      if (field.nullable && text === "") value = null;
      else {
        if (!text.trim() || !Number.isFinite(Number(text))) throw new Error("Enter a valid number.");
        value = Number(text);
      }
    }
    if (value !== field.value) changes[field.key] = value;
  }
  return changes;
}

export function configurationDirty(fields: RuntimeConfigField[], draft: ConfigurationDraft, secrets: SecretActions): boolean {
  const original = configurationDraft(fields);
  return Object.keys(original).some((key) => original[key] !== draft[key])
    || Object.values(secrets).some((action) => action !== "keep");
}
