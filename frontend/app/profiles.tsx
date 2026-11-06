"use client";

import { useI18n } from "./locale-context";

import { useState, type FormEvent } from "react";
import { errorText, segment, useAction, useQuery, type Items, type Operation, type Profile } from "./api";
import { Feedback } from "./ui";

export function Profiles({ resourceId }: { resourceId: string }) {
  const { t } = useI18n();
  const base = `/resources/${segment(resourceId)}/profiles`;
  const profiles = useQuery<Items<Profile>>(base, 2000);
  const action = useAction();
  const [name, setName] = useState("");
  const [configuration, setConfiguration] = useState('{\n  "include": [],\n  "exclude": [],\n  "pipeline": {}\n}');
  const [inputError, setInputError] = useState("");
  const [removing, setRemoving] = useState("");
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    let body: unknown;
    try { body = JSON.parse(configuration); setInputError(""); }
    catch (error) { setInputError(errorText(error)); return; }
    const receipt = await action.run<Operation>(`${base}/${segment(name.trim())}`, body, "PUT");
    if (receipt) { action.setNotice("Profile save queued. Check its result in Background tasks."); profiles.refresh(); }
  }
  async function remove() {
    const receipt = await action.run<Operation>(`${base}/${segment(removing)}`, undefined, "DELETE");
    if (receipt) { setRemoving(""); profiles.refresh(); action.setNotice("Profile deletion queued."); }
  }
  function edit(profile: Profile) {
    const { scan_mode, is_enabled, include, exclude, max_file_bytes, time_window_days, pipeline } = profile;
    setName(profile.name);
    setConfiguration(JSON.stringify({ scan_mode, is_enabled, include, exclude, max_file_bytes, time_window_days, pipeline }, null, 2));
  }
  return <div className="inline-panel"><h4>{t("Scan profiles")}</h4>
    <p>{t("Filters and processing settings are persisted. Wait for active scans before making changes. Overlapping profiles must use the same pipeline because a document has one shared index.")}</p>
    <Feedback error={profiles.error} retry={profiles.refresh} />
    {profiles.data?.items.map((profile) => <div className="button-row" key={profile.id}>
      <span>{profile.name} · {t(profile.scan_mode)} · {profile.is_enabled ? t("enabled") : t("disabled")}</span>
      <button disabled={action.busy} onClick={() => edit(profile)}>{t("Edit")}</button>
      <button disabled={action.busy || profile.name === "default"} onClick={() => setRemoving(profile.name)}>{t("Delete…")}</button>
    </div>)}
    {removing && <div className="confirmation" role="alert"><p>{t("Delete ")}<strong>{removing}</strong>{t(", its scan history, and index entries not owned by another profile? Original files are unchanged.")}</p>
      <button disabled={action.busy} onClick={remove}>{t("Confirm deletion")}</button><button onClick={() => setRemoving("")}>{t("Keep profile")}</button></div>}
    <form className="form-grid" onSubmit={save}><label>{t("Name")}<input value={name} onChange={(event) => setName(event.target.value)} required maxLength={200} disabled={action.busy} /></label>
      <label className="full">{t("Profile JSON")}<textarea rows={10} value={configuration} onChange={(event) => setConfiguration(event.target.value)} disabled={action.busy} /></label>
      <p className="muted full">{t("Saving replaces the profile configuration. Supported fields: scan_mode, is_enabled, include, exclude, max_file_bytes, time_window_days, pipeline.chunker, pipeline.parser.max_chars.")}</p>
      <Feedback error={inputError || action.error} notice={action.notice} /><button disabled={action.busy}>{t("Save profile")}</button>
    </form></div>;
}
