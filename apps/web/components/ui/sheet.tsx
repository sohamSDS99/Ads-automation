"use client";

import * as DialogPrimitive from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import * as React from "react";

import { cn } from "@/lib/utils";

export const Sheet = DialogPrimitive.Root;
export const SheetTrigger = DialogPrimitive.Trigger;
export const SheetClose = DialogPrimitive.Close;

/**
 * A full-height panel on the right edge — for a task with several sections
 * and one decision at the end of it (Stage 04 PRD §15.4 B).
 *
 * A Radix dialog underneath, so focus is trapped inside it, Escape closes it
 * and focus returns to the trigger. The header and the footer stay put and
 * the body between them scrolls: the action a person is working towards is
 * always on screen, whatever they are looking at above it. Full width on a
 * phone, where a side panel has no side to sit on.
 */
export function SheetContent({
  title,
  description,
  footer,
  children,
  className,
  ...props
}: Omit<React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content>, "title"> & {
  title: string;
  description?: string;
  footer?: React.ReactNode;
}) {
  return (
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay className="dialog-overlay fixed inset-0 z-50 bg-scrim" />
      <DialogPrimitive.Content
        className={cn(
          "sheet-surface fixed inset-y-0 right-0 z-50 flex h-dvh w-full flex-col",
          "border-l bg-surface-raised shadow-overlay sm:max-w-2xl",
          className,
        )}
        {...props}
      >
        <header className="flex items-start justify-between gap-4 border-b px-4 py-4 sm:px-6">
          <div className="min-w-0 space-y-1">
            <DialogPrimitive.Title className="text-lg font-semibold tracking-tight text-fg">
              {title}
            </DialogPrimitive.Title>
            {description ? (
              <DialogPrimitive.Description className="max-w-prose text-sm text-fg-muted">
                {description}
              </DialogPrimitive.Description>
            ) : (
              <DialogPrimitive.Description className="sr-only">{title}</DialogPrimitive.Description>
            )}
          </div>
          <DialogPrimitive.Close
            aria-label="Close"
            className="-mr-1 -mt-1 rounded-token p-1.5 text-fg-subtle transition-colors hover:bg-surface-hover hover:text-fg"
          >
            <X className="size-4" aria-hidden />
          </DialogPrimitive.Close>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
        {footer ? (
          <footer className="border-t bg-surface-raised px-4 py-3 sm:px-6">{footer}</footer>
        ) : null}
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  );
}
