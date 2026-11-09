"use client";

import { createContext, useContext, useRef, useState, type FormEvent, type ReactNode } from "react";
import Image from "next/image";
import { segment, useAction, useQuery, type UserProfile } from "./api";
import { useI18n } from "./locale-context";
import { Feedback } from "./ui";

type ProfileContext = {
  workspace: string;
  profile?: UserProfile;
  error: string;
  refresh: () => void;
  accept: (profile: UserProfile) => void;
};
const Context = createContext<ProfileContext | null>(null);

export function UserProfileProvider({ workspace = "", children }: { workspace?: string; children: ReactNode }) {
  const path = workspace ? `/user/profile?workspace=${segment(workspace)}` : null;
  const query = useQuery<UserProfile>(path, 10000);
  const [saved, setSaved] = useState<{ workspace: string; profile: UserProfile }>();
  const observed = query.dataPath === path ? query.data : undefined;
  const acknowledged = saved?.workspace === workspace ? saved.profile : undefined;
  const profile = acknowledged && (!observed || acknowledged.revision >= observed.revision) ? acknowledged : observed;
  return <Context.Provider value={{
    workspace, profile, error: query.errorPath === path ? query.error : "", refresh: query.refresh,
    accept: (profile) => { setSaved({ workspace, profile }); query.refresh(); },
  }}>{children}</Context.Provider>;
}

export function useUserProfile() {
  const context = useContext(Context);
  if (!context) throw new Error("User profile requires UserProfileProvider.");
  return context;
}

export function UserAvatar({ src = "" }: { src?: string }) {
  const { t } = useI18n();
  const [failed, setFailed] = useState("");
  return src && failed !== src
    ? <Image src={src} width={32} height={32} alt="" unoptimized onError={() => setFailed(src)} />
    : <svg aria-label={src ? t("Avatar unavailable") : undefined} width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="8" r="3.5" /><path d="M5 21v-2a7 7 0 0 1 14 0v2" /></svg>;
}

export function UserProfileSettings() {
  const { t } = useI18n();
  const { workspace, profile, error, refresh } = useUserProfile();
  return <section className="settings-card" aria-labelledby="user-profile-heading">
    <header className="settings-card-header"><div><h2 id="user-profile-heading">{t("User profile")}</h2><p className="muted">{t("Saved in this workspace. The main Agent uses your current profile and nickname. Chat does not change these settings.")}</p></div></header>
    <Feedback error={error} retry={refresh} />
    {!profile && !error && <p role="status">{t("Loading profile…")}</p>}
    {profile && <ProfileEditor key={workspace} initial={profile} />}
  </section>;
}

function ProfileEditor({ initial }: { initial: UserProfile }) {
  const { t } = useI18n();
  const { profile, accept } = useUserProfile();
  const [config, setConfig] = useState(initial);
  const [draft, setDraft] = useState(initial);
  const [avatarData, setAvatarData] = useState<string | null>();
  const [reading, setReading] = useState(false);
  const [avatarError, setAvatarError] = useState("");
  const [confirmReload, setConfirmReload] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const action = useAction();
  const dirty = avatarData !== undefined || (["nickname", "gender", "occupation", "bio"] as const).some(key => draft[key] !== config[key]);
  const busy = action.busy || reading;
  const avatar = avatarData === undefined ? config.avatar_url : avatarData || "";
  const changedElsewhere = profile && profile.revision > config.revision;

  function apply(saved: UserProfile) {
    setConfig(saved); setDraft(saved); setAvatarData(undefined); setAvatarError(""); setConfirmReload(false);
    if (fileInput.current) fileInput.current.value = "";
    accept(saved);
  }
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const saved = await action.run<UserProfile>("/user/profile", {
      revision: config.revision, nickname: draft.nickname, gender: draft.gender,
      occupation: draft.occupation, bio: draft.bio,
      ...(avatarData !== undefined ? { avatar_data: avatarData } : {}),
    }, "PUT");
    if (saved) {
      apply(saved);
      action.setNotice("Profile saved. The next message uses your updated profile.");
    }
  }
  async function reload() {
    const saved = await action.run<UserProfile>("/user/profile", undefined, "GET");
    if (saved) apply(saved);
  }
  async function chooseAvatar(file?: File) {
    if (!file) return;
    setAvatarError("");
    if (file.size > 2 * 1024 * 1024 || !["image/png", "image/jpeg", "image/webp"].includes(file.type)) {
      setAvatarError("Choose a PNG, JPEG or WebP image of at most 2 MB.");
      return;
    }
    setReading(true);
    try {
      const value = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => typeof reader.result === "string" ? resolve(reader.result) : reject(new Error("Invalid image"));
        reader.onerror = () => reject(reader.error);
        reader.onabort = () => reject(new Error("Image read cancelled"));
        reader.readAsDataURL(file);
      });
      setAvatarData(value);
    } catch {
      setAvatarError("Could not read the avatar. Choose the file again.");
    } finally { setReading(false); }
  }
  return <form className="user-profile-editor" onSubmit={save}>
    <div className="profile-avatar-row"><span className="profile-avatar" aria-hidden="true"><UserAvatar src={avatar} /></span><div>
      <label htmlFor="profile-avatar">{t("Avatar")}</label>
      <input ref={fileInput} id="profile-avatar" type="file" accept="image/png,image/jpeg,image/webp" disabled={busy} onChange={event => void chooseAvatar(event.target.files?.[0])} aria-describedby="profile-avatar-help" />
      <p className="muted" id="profile-avatar-help">{t("PNG, JPEG or WebP, up to 2 MB and 4 megapixels. Saved as a small static image without metadata.")}</p>
      <button type="button" disabled={busy || !avatar} onClick={() => { setAvatarData(null); setAvatarError(""); if (fileInput.current) fileInput.current.value = ""; }}>{t("Remove avatar")}</button>
    </div></div>
    <Feedback error={avatarError} />
    <label htmlFor="profile-nickname">{t("Nickname")}<input id="profile-nickname" autoComplete="nickname" maxLength={80} value={draft.nickname} disabled={busy} onChange={event => setDraft({ ...draft, nickname: event.target.value })} /></label>
    <label htmlFor="profile-gender">{t("Gender")}<input id="profile-gender" maxLength={80} value={draft.gender} disabled={busy} onChange={event => setDraft({ ...draft, gender: event.target.value })} /></label>
    <label htmlFor="profile-occupation">{t("Occupation")}<input id="profile-occupation" autoComplete="organization-title" maxLength={200} value={draft.occupation} disabled={busy} onChange={event => setDraft({ ...draft, occupation: event.target.value })} /></label>
    <label htmlFor="profile-bio">{t("About me")}<textarea id="profile-bio" maxLength={1000} rows={4} value={draft.bio} disabled={busy} onChange={event => setDraft({ ...draft, bio: event.target.value })} /></label>
    <p className="muted">{t("All fields are optional. Clearing a field also removes its settings-managed Memory. Your profile remains available when automatic Memory is off.")}</p>
    {changedElsewhere && <p className="warning" role="status">{t("Profile changed elsewhere. Reload before saving; your edits are kept until you discard them.")}</p>}
    <div className="button-row">
      <button className="primary" type="submit" disabled={busy || Boolean(avatarError) || (!dirty && config.revision > 0) || Boolean(changedElsewhere)}>{busy ? t("Saving…") : t("Save user profile")}</button>
      <button type="button" disabled={busy} onClick={() => dirty ? setConfirmReload(true) : void reload()}>{t("Reload saved profile")}</button>
      {dirty && <span className="badge">{t("Unsaved changes")}</span>}
    </div>
    {confirmReload && <div className="confirmation" role="alert"><p>{t("Discard unsaved profile changes and reload?")}</p><div className="button-row"><button type="button" disabled={busy} onClick={reload}>{t("Discard edits and reload")}</button><button type="button" onClick={() => setConfirmReload(false)}>{t("Keep editing")}</button></div></div>}
    <Feedback error={action.error} notice={action.notice} />
    {action.error && <p className="muted">{t("If the response was lost, reload the saved profile before retrying.")}</p>}
  </form>;
}
