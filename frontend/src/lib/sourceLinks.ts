export type GitHubRepoParts = {
  owner: string;
  repo: string;
  subpath: string;
};

export function parseGitHubRepoId(repoId?: string | null): GitHubRepoParts | null {
  if (!repoId) return null;
  const parts = repoId.split("/").filter(Boolean);
  if (parts.length < 2) return null;
  return {
    owner: parts[0],
    repo: parts[1],
    subpath: parts.slice(2).join("/"),
  };
}

export function githubRepoRootUrl(repoId?: string | null): string | null {
  const parts = parseGitHubRepoId(repoId);
  if (!parts) return null;
  return `https://github.com/${parts.owner}/${parts.repo}`;
}

export function githubRepoRootLabel(repoId: string): string {
  const parts = parseGitHubRepoId(repoId);
  if (!parts) return repoId;
  return `${parts.owner}/${parts.repo}`;
}

export function githubTreeUrl(repoId?: string | null): string | null {
  return githubRepoRootUrl(repoId);
}

export function githubEvidenceUrl(repoId: string | undefined, path: string, line?: number): string | null {
  const parts = parseGitHubRepoId(repoId);
  if (!parts || !path) return null;
  const suffix = line ? `#L${line}` : "";
  return `https://github.com/${parts.owner}/${parts.repo}/blob/main/${path}${suffix}`;
}
