"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { FolderPlus } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Spinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import { ApiError } from "@/lib/api";
import { createProject } from "@/lib/api/projects";
import { keys } from "@/lib/queries";

/**
 * Name it, then set it up.
 *
 * Two fields, because a project that exists is something you can come back to
 * and a wizard you abandoned on step one is not. Everything else is collected
 * by the setup flow this hands off to.
 */
export function NewProjectDialog() {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [domain, setDomain] = useState("");
  const [error, setError] = useState<string | null>(null);
  const router = useRouter();
  const queryClient = useQueryClient();

  const create = useMutation({
    mutationFn: () => createProject(name.trim(), domain.trim()),
    onSuccess: async (project) => {
      await queryClient.invalidateQueries({ queryKey: keys.projects });
      setOpen(false);
      setName("");
      setDomain("");
      toast.success(`${project.name} created`, { description: "Now tell it what to research." });
      router.push(`/projects/${project.id}/setup`);
    },
    onError: (err) => {
      setError(
        err instanceof ApiError
          ? err.detail
          : "The project could not be created. Try again in a moment.",
      );
    },
  });

  return (
    <>
      <Button onClick={() => setOpen(true)}>
        <FolderPlus aria-hidden />
        New project
      </Button>

      <Dialog open={open} onOpenChange={create.isPending ? undefined : setOpen}>
        <DialogContent
          title="New project"
          description="One brand and the domain its campaigns point at."
        >
          <form
            onSubmit={(event) => {
              event.preventDefault();
              setError(null);
              create.mutate();
            }}
          >
            <DialogBody>
              <Field
                label="Name"
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="SDS Manager — Nordics"
                autoFocus
                required
                maxLength={120}
                hint="What your team calls this piece of work."
              />
              <Field
                label="Domain"
                value={domain}
                onChange={(event) => setDomain(event.target.value)}
                placeholder="sdsmanager.com"
                required
                error={error ?? undefined}
                hint="Paste a URL if that is easier — it will be trimmed to the hostname."
              />
            </DialogBody>
            <DialogFooter>
              <Button
                type="button"
                variant="secondary"
                onClick={() => setOpen(false)}
                disabled={create.isPending}
              >
                Cancel
              </Button>
              <Button type="submit" disabled={create.isPending || !name.trim() || !domain.trim()}>
                {create.isPending ? <Spinner label="Creating" /> : null}
                Create and set up
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}
