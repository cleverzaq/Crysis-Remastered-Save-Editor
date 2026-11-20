"""Crysis 3 profile-specific GUI controls used by CrysisSaveEditor.

Separate tab to avoid crowding the XML tree and game-profile attribute editor.
"""
from __future__ import annotations

import tkinter as tk
import xml.etree.ElementTree as ET
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from crysis3_stats import (
    DIFFICULTIES, read_campaign_statistics,
    read_distributions, read_perks, read_upgrade_kits,
    set_distribution, set_mission_difficulty, set_perk, set_playtime,
    set_upgrade_kit, set_upgrade_points,
)

from weapon_attachments import (
    ATTACHMENTS, export_detected_csv, inspect_profile, summary,
    set_attachment_unlocked, unlock_all_attachments,
)


class Crysis3ProgressMixin:
    def _build_crysis3_features(self):
        top = ttk.Frame(self.crysis3_tab)
        top.pack(fill='x', pady=(0, 8))
        ttk.Label(top, text='Crysis 3 Remastered  |  Campaign & Nanosuit',
                  font=('Segoe UI Semibold', 12)).pack(side='left')
        ttk.Button(top, text='Open Profile', command=self.open_profile).pack(side='right', padx=(5, 0))
        ttk.Button(top, text='Save Encrypted Profile', command=self.save_profile).pack(side='right', padx=(5, 0))
        ttk.Button(top, text='Refresh', command=self._refresh_profile_editor).pack(side='right', padx=(5, 0))
        self.c3_status = tk.StringVar(value='Open an encrypted Crysis 3 Remastered attributes.xml in Game Profile Editor.')
        ttk.Label(self.crysis3_tab, textvariable=self.c3_status, style='Muted.TLabel',
                  wraplength=1050).pack(fill='x', pady=(0, 9))
        self.c3_tabs = ttk.Notebook(self.crysis3_tab)
        self.c3_tabs.pack(fill='both', expand=True)
        self.c3_campaign_page = ttk.Frame(self.c3_tabs, padding=12)
        self.c3_modules_page = ttk.Frame(self.c3_tabs, padding=12)
        self.c3_perks_page = ttk.Frame(self.c3_tabs, padding=12)
        self.c3_kits_page = ttk.Frame(self.c3_tabs, padding=12)
        self.c3_attachments_page = ttk.Frame(self.c3_tabs, padding=12)
        self.c3_tabs.add(self.c3_campaign_page, text='Campaign Statistics')
        self.c3_tabs.add(self.c3_modules_page, text='Nanosuit Modules')
        self.c3_tabs.add(self.c3_perks_page, text='Perk Levels')
        self.c3_tabs.add(self.c3_kits_page, text='Upgrade Kits')
        self.c3_tabs.add(self.c3_attachments_page, text='Weapon Attachments (20)')
        self._build_c3_campaign()
        self._build_c3_modules()
        self._build_c3_perks()
        self._build_c3_kits()
        self._build_c3_attachments()
        self._refresh_crysis3_features()

    def _c3_tree(self, container, columns, headings, widths, height=9):
        holder = ttk.Frame(container)
        holder.pack(fill='both', expand=True, pady=(5, 8))
        tree = ttk.Treeview(holder, columns=columns, show='headings', selectmode='browse', height=height)
        for key, title, width in zip(columns, headings, widths):
            tree.heading(key, text=title)
            tree.column(key, width=width, stretch=True, anchor='w')
        scrollbar = ttk.Scrollbar(holder, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        return tree

    def _build_c3_campaign(self):
        frame = self.c3_campaign_page
        stats = ttk.LabelFrame(frame, text='Campaign Statistics', padding=10)
        stats.pack(fill='x', pady=(0, 10))
        self.c3_time_var = tk.StringVar()
        self.c3_progress_var = tk.StringVar(value='No profile loaded')
        line = ttk.Frame(stats)
        line.pack(fill='x', pady=(0, 5))
        ttk.Label(line, text='Total campaign play time (H:MM:SS):').pack(side='left')
        ttk.Entry(line, textvariable=self.c3_time_var, width=15).pack(side='left', padx=8)
        ttk.Button(line, text='Apply Play Time', command=self._c3_apply_time).pack(side='left', padx=(0, 8))
        ttk.Label(line, text='Updates only EIPS_TimePlayed', style='Muted.TLabel').pack(side='left')
        ttk.Label(stats, textvariable=self.c3_progress_var, justify='left',
                  wraplength=1000).pack(anchor='w', pady=4)
        missions = ttk.LabelFrame(frame, text='Completed Missions by Highest Difficulty', padding=10)
        missions.pack(fill='both', expand=True, pady=(0, 9))
        ttk.Label(missions, text='Select a mission, choose the highest difficulty completed, then Apply. '
                  'Lower difficulty totals are inferred from the highest completed difficulty.',
                  style='Muted.TLabel', wraplength=1040).pack(anchor='w')
        self.c3_mission_tree = self._c3_tree(
            missions, ('level', 'mission', 'difficulty'),
            ('Level ID', 'Mission', 'Highest completed difficulty'), (125, 260, 280), height=7)
        self.c3_mission_tree.bind('<<TreeviewSelect>>', self._c3_pick_mission)
        actions = ttk.Frame(missions)
        actions.pack(fill='x')
        ttk.Label(actions, text='Highest difficulty:').pack(side='left')
        self.c3_mission_difficulty = tk.StringVar(value='0 - Not completed')
        choices = ['0 - Not completed'] + [f'{n} - {label}' for label, n in DIFFICULTIES]
        ttk.Combobox(actions, textvariable=self.c3_mission_difficulty,
                     values=choices, state='readonly', width=28).pack(side='left', padx=7)
        ttk.Button(actions, text='Apply Mission Completion',
                   command=self._c3_apply_mission).pack(side='left')
        limitations = ttk.LabelFrame(frame, text='Collectibles & Completion Percentage', padding=10)
        limitations.pack(fill='x')
        ttk.Label(limitations,
                  text='Data pads, propaganda posters, black boxes and the overall completion percentage '
                       'cannot currently be mapped to verified editable fields in the provided profile. '
                       'The editor will not guess or overwrite those figures.',
                  style='Muted.TLabel', wraplength=1040).pack(anchor='w')

    def _build_c3_modules(self):
        frame = self.c3_modules_page
        summary = ttk.LabelFrame(frame, text='Nanosuit Summary', padding=10)
        summary.pack(fill='x', pady=(0, 8))
        self.c3_module_summary = tk.StringVar(value='Open a profile to read purchased modules.')
        ttk.Label(summary, textvariable=self.c3_module_summary).pack(anchor='w', pady=(0, 6))
        points = ttk.Frame(summary)
        points.pack(fill='x')
        ttk.Label(points, text='Available Upgrade Points:').pack(side='left')
        self.c3_upgrade_points = tk.StringVar()
        ttk.Entry(points, textvariable=self.c3_upgrade_points, width=14).pack(side='left', padx=7)
        ttk.Button(points, text='Apply Points', command=self._c3_apply_points).pack(side='left')
        ttk.Label(summary, text='Module purchase flags and spent points are separate game fields; '
                  'changing one does not automatically rebalance the other.',
                  style='Muted.TLabel', wraplength=990).pack(anchor='w', pady=(7, 0))
        box = ttk.LabelFrame(frame, text='Module Purchase / Unlock Flags (PerkDistribution)', padding=10)
        box.pack(fill='both', expand=True)
        actions = ttk.Frame(box)
        actions.pack(fill='x', pady=(2, 0))
        self.c3_dist_purchased = tk.StringVar(value='0')
        self.c3_dist_unlocked = tk.StringVar(value='0')
        self.c3_dist_max = tk.StringVar(value='0')
        for title, var in (('Purchased', self.c3_dist_purchased), ('Unlocked', self.c3_dist_unlocked),
                           ('MaximumTier', self.c3_dist_max)):
            ttk.Label(actions, text=title+':').pack(side='left', padx=(0, 4))
            ttk.Combobox(actions, textvariable=var, values=('0','1'), state='readonly',
                         width=4).pack(side='left', padx=(0, 15))
        ttk.Button(actions, text='Apply Module Flags', command=self._c3_apply_distribution,
                   style='Accent.TButton').pack(side='left')
        self.c3_distribution_tree = self._c3_tree(
            box, ('name', 'purchased', 'unlocked', 'max_tier'),
            ('Module', 'Purchased', 'Unlocked', 'MaximumTier'), (220, 105, 105, 130), height=12)
        self.c3_distribution_tree.bind('<<TreeviewSelect>>', self._c3_pick_distribution)

    def _build_c3_perks(self):
        frame = self.c3_perks_page
        ttk.Label(frame, text='PerkTier and PerkActive from the current profile. '
                  'Tier is limited to 0..4; Active to 0 or 1.',
                  style='Muted.TLabel', wraplength=1000).pack(anchor='w', pady=(0, 8))
        controls = ttk.Frame(frame)
        controls.pack(fill='x', pady=7)
        self.c3_perk_tier = tk.StringVar(value='0')
        self.c3_perk_active = tk.StringVar(value='0')
        ttk.Label(controls, text='Tier:').pack(side='left')
        ttk.Combobox(controls, textvariable=self.c3_perk_tier, values=('0','1','2','3','4'),
                     state='readonly', width=5).pack(side='left', padx=(5, 18))
        ttk.Label(controls, text='Active:').pack(side='left')
        ttk.Combobox(controls, textvariable=self.c3_perk_active, values=('0','1'),
                     state='readonly', width=5).pack(side='left', padx=(5, 18))
        ttk.Button(controls, text='Apply Perk', command=self._c3_apply_perk,
                   style='Accent.TButton').pack(side='left')
        self.c3_perks_tree = self._c3_tree(
            frame, ('perk', 'tier', 'active'), ('Perk', 'Tier', 'Active'), (340, 120, 120), height=18)
        self.c3_perks_tree.bind('<<TreeviewSelect>>', self._c3_pick_perk)

    def _build_c3_kits(self):
        frame = self.c3_kits_page
        ttk.Label(frame, text='Upgrade-kit flags stored under NanosuitUpgrades/. '
                  'Editing a flag does not spawn an item or modify the actual CSF save.',
                  style='Muted.TLabel', wraplength=1020).pack(anchor='w')
        kits = ttk.LabelFrame(frame, text='Upgrade Kit Collection Flags', padding=10)
        kits.pack(fill='both', expand=True, pady=8)
        controls = ttk.Frame(kits)
        controls.pack(fill='x')
        self.c3_kit_collected = tk.StringVar(value='0')
        ttk.Label(controls, text='Collected:').pack(side='left')
        ttk.Combobox(controls, textvariable=self.c3_kit_collected, values=('0','1'),
                     state='readonly', width=5).pack(side='left', padx=8)
        ttk.Button(controls, text='Apply Kit Flag', command=self._c3_apply_kit).pack(side='left')
        self.c3_kit_tree = self._c3_tree(
            kits, ('location', 'collected'), ('Profile attribute', 'Collected'), (550, 90), height=10)
        self.c3_kit_tree.bind('<<TreeviewSelect>>', self._c3_pick_kit)

    def _c3_root(self):
        doc = self.profile_document
        if doc is None or getattr(self, 'profile_game', '') != 'Crysis3Remastered':
            messagebox.showinfo('Crysis 3 Profile Required',
                                'First open an encrypted Crysis 3 Remastered attributes.xml in Game Profile Editor.')
            return None
        return doc.root

    def _c3_commit(self, fn, *args):
        root = self._c3_root()
        if root is None:
            return
        try:
            changed = fn(root, *args)
        except (ValueError, TypeError) as exc:
            messagebox.showerror('Invalid Crysis 3 Value', str(exc))
            return
        self._refresh_profile_editor()
        self.status_var.set((f'Updated {len(changed)} Crysis 3 profile field(s) in memory. '
                             'Use Save Encrypted Profile to persist changes.')
                            if changed else 'Values already match the loaded Crysis 3 profile.')

    def _c3_selected(self, tree, label):
        selection = tree.selection()
        if not selection:
            messagebox.showinfo(label, 'Select a row first.')
            return None
        return selection[0]

    def _c3_pick_mission(self, _event=None):
        sel = self.c3_mission_tree.selection()
        if sel:
            value = self.c3_mission_tree.set(sel[0], 'difficulty').split(' - ', 1)[0]
            choice = {str(n): f'{n} - {label}' for label, n in DIFFICULTIES}
            choice['0'] = '0 - Not completed'
            self.c3_mission_difficulty.set(choice.get(value, '0 - Not completed'))

    def _c3_apply_mission(self):
        key = self._c3_selected(self.c3_mission_tree, 'Mission Completion')
        if key is not None:
            value = self.c3_mission_difficulty.get().split(' - ', 1)[0]
            self._c3_commit(set_mission_difficulty, key, value)

    def _c3_apply_time(self):
        self._c3_commit(set_playtime, self.c3_time_var.get())

    def _c3_apply_points(self):
        self._c3_commit(set_upgrade_points, self.c3_upgrade_points.get())

    def _c3_pick_distribution(self, _event=None):
        sel = self.c3_distribution_tree.selection()
        if sel:
            values = self.c3_distribution_tree.item(sel[0], 'values')
            self.c3_dist_purchased.set(str(values[1]))
            self.c3_dist_unlocked.set(str(values[2]))
            self.c3_dist_max.set(str(values[3]))

    def _c3_apply_distribution(self):
        key = self._c3_selected(self.c3_distribution_tree, 'Nanosuit Module')
        if key is not None:
            self._c3_commit(set_distribution, key, self.c3_dist_purchased.get(),
                            self.c3_dist_unlocked.get(), self.c3_dist_max.get())

    def _c3_pick_perk(self, _event=None):
        sel = self.c3_perks_tree.selection()
        if sel:
            values = self.c3_perks_tree.item(sel[0], 'values')
            self.c3_perk_tier.set(str(values[1]))
            self.c3_perk_active.set(str(values[2]) if str(values[2]) in ('0','1') else '0')

    def _c3_apply_perk(self):
        key = self._c3_selected(self.c3_perks_tree, 'Perk')
        if key is not None:
            self._c3_commit(set_perk, key, self.c3_perk_tier.get(), self.c3_perk_active.get())

    def _c3_pick_kit(self, _event=None):
        sel = self.c3_kit_tree.selection()
        if sel:
            self.c3_kit_collected.set(str(self.c3_kit_tree.item(sel[0], 'values')[1]))

    def _c3_apply_kit(self):
        key = self._c3_selected(self.c3_kit_tree, 'Upgrade Kit')
        if key is not None:
            self._c3_commit(set_upgrade_kit, key, self.c3_kit_collected.get())

    @staticmethod
    def _c3_replace_tree(tree, rows):
        selected = tree.selection()
        tree.delete(*tree.get_children())
        for iid, vals in rows:
            tree.insert('', 'end', iid=iid, values=vals)
        if selected and tree.exists(selected[0]):
            tree.selection_set(selected[0])
            tree.see(selected[0])

    def _refresh_crysis3_features(self):
        if not hasattr(self, 'c3_status'):
            return
        doc = self.profile_document
        if doc is None or getattr(self, 'profile_game', '') != 'Crysis3Remastered':
            self.c3_status.set('Open an encrypted Crysis 3 Remastered attributes.xml via Game Profile Editor first.')
            for tree in (self.c3_mission_tree, self.c3_distribution_tree,
                         self.c3_perks_tree, self.c3_kit_tree):
                tree.delete(*tree.get_children())
            self.c3_progress_var.set('No Crysis 3 profile loaded.')
            self.c3_module_summary.set('No Crysis 3 profile loaded.')
            self.c3_time_var.set('')
            self.c3_upgrade_points.set('')
            self._c3_refresh_attachments()
            return
        try:
            root = doc.root
            stats = read_campaign_statistics(root)
            distributions = read_distributions(root)
            perks = read_perks(root)
            kits = read_upgrade_kits(root)
        except ValueError as exc:
            self.c3_status.set(f'Cannot parse profile statistics: {exc}')
            return
        self.c3_status.set(f'{doc.path}  |  Edit values, click Apply, then Save Encrypted Profile (backup + verification).')
        self.c3_time_var.set(stats['time_display'] if stats['time_seconds'] is not None else '')
        self.c3_progress_var.set('     |     '.join(
            f'{label}: {count}/{total}' for label, _level, count, total in stats['progress']))
        diff_name = {n: label for label, n in DIFFICULTIES}
        diff_name[0] = 'Not completed'
        self._c3_replace_tree(self.c3_mission_tree, [
            (level, (level, name, f'{highest} - {diff_name[highest]}'))
            for level, name, highest in stats['missions']])
        purchased = sum(d['purchased'] for d in distributions)
        self.c3_module_summary.set(
            f'Purchased modules: {purchased}/{len(distributions)}    |    '
            f'Points accumulated: {stats["points_accumulated"] if stats["points_accumulated"] is not None else "N/A"}')
        self.c3_upgrade_points.set(str(stats['points_available']) if stats['points_available'] is not None else '')
        self._c3_replace_tree(self.c3_distribution_tree, [
            (item['id'], (item['id'], item['purchased'], item['unlocked'], item['max_tier']))
            for item in distributions])
        self._c3_replace_tree(self.c3_perks_tree, [
            (item['id'], (item['id'], item['tier'],
                          item['active'] if item['active'] is not None else '-'))
            for item in perks])
        self._c3_replace_tree(self.c3_kit_tree, [
            (key, (key, value)) for key, value in kits])
        self._c3_refresh_attachments()


    def _build_c3_attachments(self):
        frame = self.c3_attachments_page
        self.c3_attach_external_root = None
        self.c3_attach_external_path = None
        self.c3_attach_external_active = False
        self.c3_attach_seen_profile_path = None
        self.c3_attach_diag = inspect_profile(None)

        intro = ttk.LabelFrame(frame, text='Geared-up: 20 weapon attachments', padding=9)
        intro.pack(fill='x', pady=(0, 7))
        ttk.Label(intro, text='AUTO DETECTION: Compares 20 attachment keys in '
                  'EMPS_AttachmentUnlocked/init with known keys from a completed 100% profile. '
                  '19/20 vs 100% comparison identified the missing extendedclipfeline key. '
                  'The status is inferred from a reference profile, not the game executable. '
                  'Unlock buttons edit the opened encrypted profile in memory until you save it.',
                  wraplength=1040, style='Muted.TLabel', justify='left').pack(fill='x')
        bar = ttk.Frame(intro)
        bar.pack(fill='x', pady=(7, 0))
        self.c3_attach_summary = tk.StringVar(value='Load a Crysis 3 profile to detect attachments automatically.')
        ttk.Label(bar, textvariable=self.c3_attach_summary, font=('Segoe UI Semibold', 10)).pack(side='left')
        ttk.Label(intro, text='Validated missing key in your earlier profile: '
                  'extendedclipfeline (Feline X3 extended magazine, Welcome to the Jungle).',
                  wraplength=1040, style='Muted.TLabel').pack(fill='x', pady=(5, 0))
        self.c3_attach_data_info = tk.StringVar(value='Load a game profile or exported XML for diagnostics.')
        ttk.Label(intro, textvariable=self.c3_attach_data_info, style='Muted.TLabel', wraplength=1000,
                  justify='left').pack(fill='x', pady=(6, 0))

        actions = ttk.Frame(frame)
        actions.pack(fill='x', pady=(2, 5))
        ttk.Label(actions, text='Search:').pack(side='left')
        self.c3_attach_search_var = tk.StringVar()
        query = ttk.Entry(actions, textvariable=self.c3_attach_search_var, width=26)
        query.pack(side='left', padx=(5, 15))
        self.c3_attach_search_var.trace_add('write', lambda *_: self._c3_attach_redraw())
        ttk.Label(actions, text='Show:').pack(side='left')
        self.c3_attach_filter_var = tk.StringVar(value='All')
        filters = ttk.Combobox(actions, textvariable=self.c3_attach_filter_var, state='readonly',
                               values=('All', 'Missing', 'Acquired'), width=12)
        filters.pack(side='left', padx=6)
        filters.bind('<<ComboboxSelected>>', lambda _e: self._c3_attach_redraw())
        ttk.Button(actions, text='Use Open Profile',
                   command=self._c3_attach_use_profile).pack(side='right', padx=(5, 0))
        ttk.Button(actions, text='Import Exported XML (Read-only)',
                   command=self._c3_attach_open_export).pack(side='right', padx=(5, 0))
        ttk.Button(actions, text='Export CSV', command=self._c3_attach_export).pack(side='right', padx=(5, 0))

        listing = ttk.LabelFrame(frame, text='Attachment list - select an item to view where to find it', padding=8)
        listing.pack(fill='both', expand=True, pady=(0, 7))
        self.c3_attach_tree = self._c3_tree(
            listing, ('number', 'attachment', 'weapon', 'mission', 'auto'),
            ('#', 'Attachment', 'Weapon', 'Mission', 'Detected in XML'),
            (45, 245, 195, 210, 150), height=6)
        self.c3_attach_tree.bind('<<TreeviewSelect>>', self._c3_attach_select)

        profilebar = ttk.Frame(frame)
        profilebar.pack(fill='x', pady=(0, 5))
        ttk.Label(profilebar, text='Edit game profile:').pack(side='left')
        ttk.Button(profilebar, text='Unlock Selected', command=lambda: self._c3_attach_edit_profile(True),
                   style='Accent.TButton').pack(side='left', padx=(7, 4))
        ttk.Button(profilebar, text='Lock Selected', command=lambda: self._c3_attach_edit_profile(False)).pack(side='left', padx=4)
        ttk.Button(profilebar, text='Unlock All 20', command=self._c3_attach_unlock_all).pack(side='left', padx=4)
        ttk.Label(profilebar, text='Creates missing entries in new profiles; save separately',
                  style='Muted.TLabel').pack(side='left', padx=(10, 0))

        detail = ttk.LabelFrame(frame, text='Selected attachment / location hint', padding=8)
        detail.pack(fill='x')
        self.c3_attach_detail_var = tk.StringVar(value='Select one of the 20 attachments.')
        ttk.Label(detail, textvariable=self.c3_attach_detail_var, wraplength=1050,
                  justify='left').pack(fill='x')

    def _c3_attach_context(self):
        if self.c3_attach_external_active and self.c3_attach_external_root is not None:
            return self.c3_attach_external_root, self.c3_attach_external_path
        doc = getattr(self, 'profile_document', None)
        if doc is not None and getattr(self, 'profile_game', '') == 'Crysis3Remastered':
            return doc.root, doc.path
        if self.c3_attach_external_root is not None:
            return self.c3_attach_external_root, self.c3_attach_external_path
        return None, None

    def _c3_refresh_attachments(self):
        # If another encrypted profile is opened, switch back to its live contents.
        doc = getattr(self, 'profile_document', None)
        if doc is not None and getattr(self, 'profile_game', '') == 'Crysis3Remastered':
            seen = str(doc.path)
            if self.c3_attach_seen_profile_path != seen:
                self.c3_attach_external_active = False
                self.c3_attach_seen_profile_path = seen
        root, path = self._c3_attach_context()
        try:
            self.c3_attach_diag = inspect_profile(root)
        except ValueError as exc:
            self.c3_attach_diag = inspect_profile(None)
            self.c3_attach_data_info.set(f'Profile diagnostic error: {exc}')
        else:
            count = self.c3_attach_diag['initializer_count']
            flags = len(self.c3_attach_diag['explicit_flags'])
            source = str(path) if path is not None else 'No profile loaded'
            auto = self.c3_attach_diag.get('auto_states', {})
            self.c3_attach_data_info.set(
                f'Source: {source}  |  Raw initializer entries: {count} '
                f'|  20-key auto status: {sum(v == "acquired" for v in auto.values())}/20 '
                f'|  Direct flags: {flags}. '
                'Inferred from comparison with the completed profile.')
        self._c3_attach_redraw()

    def _c3_attach_redraw(self):
        if not hasattr(self, 'c3_attach_tree'):
            return
        selected = self.c3_attach_tree.selection()
        current = selected[0] if selected else None
        text = self.c3_attach_search_var.get().strip().casefold()
        only = self.c3_attach_filter_var.get()
        self.c3_attach_tree.delete(*self.c3_attach_tree.get_children())
        auto = self.c3_attach_diag.get('auto_states', {})
        for attachment in ATTACHMENTS:
            detected = auto.get(attachment.number, 'unknown')
            if only != 'All' and only.casefold() != (detected if detected != 'unknown' else 'unchecked'):
                continue
            if text and text not in (' '.join((attachment.name, attachment.weapon,
                                             attachment.mission, attachment.location,
                                             attachment.profile_key or ''))).casefold():
                continue
            self.c3_attach_tree.insert('', 'end', iid=str(attachment.number),
                                       values=(attachment.number, attachment.name, attachment.weapon,
                                               attachment.mission,
                                               'Not loaded' if detected == 'unknown' else detected.title()))
        if current and self.c3_attach_tree.exists(current):
            self.c3_attach_tree.selection_set(current)
            self.c3_attach_tree.see(current)
        progress = summary(auto)
        self.c3_attach_summary.set(f'Profile detected: {progress["acquired"]}/20  |  '
                                   f'Missing: {progress["missing"]}  |  '
                                   f'Unknown: {progress["unknown"]}')

    def _c3_attach_select(self, _event=None):
        sel = self.c3_attach_tree.selection()
        if not sel:
            return
        number = int(sel[0])
        attachment = ATTACHMENTS[number - 1]
        key = attachment.profile_key or 'unknown'
        detected = self.c3_attach_diag.get('auto_states', {}).get(number, 'unknown')
        self.c3_attach_detail_var.set(
            f'#{number}  {attachment.name}  |  {attachment.weapon}  |  {attachment.mission}\n'
            f'{attachment.location}\nXML key: {key}  |  '
            f'Detected: {detected} (inferred from 100% reference).')

    def _c3_attach_live_root(self):
        """Only allow profile changes in the live encrypted Crysis 3 document."""
        if self.c3_attach_external_active:
            messagebox.showinfo('Read-only XML',
                'Imported XML is for diagnostics only. Select Use Open Profile, then unlock the attachment.')
            return None
        return self._c3_root()

    def _c3_attach_edit_profile(self, unlocked: bool):
        selected = self.c3_attach_tree.selection()
        if not selected:
            messagebox.showinfo('Select attachment', 'Select an attachment from the table first.')
            return
        root = self._c3_attach_live_root()
        if root is None:
            return
        number = int(selected[0])
        attachment = ATTACHMENTS[number - 1]
        if not unlocked and not messagebox.askyesno('Lock Weapon Attachment',
            f'Remove {attachment.profile_key} from the unlock initializer?\n'
            'This may make the attachment unavailable after the profile is saved. ' 
            'A backup is created when saving the encrypted profile.'):
            return
        try:
            changes = set_attachment_unlocked(root, number, unlocked=unlocked)
        except ValueError as exc:
            messagebox.showerror('Cannot edit attachment unlock', str(exc))
            return
        self._refresh_profile_editor()
        self.status_var.set((f'{"Unlocked" if unlocked else "Locked"}: {attachment.name} '
            f'({attachment.profile_key}) in memory. Click Save Encrypted Profile to save.')
            if changes else f'{attachment.name} already has the requested unlock state.')

    def _c3_attach_unlock_all(self):
        root = self._c3_attach_live_root()
        if root is None:
            return
        if not messagebox.askyesno('Unlock All Weapon Attachments',
            'Add any missing of the 20 known unlock identifiers to this profile?\n'
            'This works on new profiles even if their unlock list is empty or absent.\n\n'
            'Changes stay in memory until Save Encrypted Profile, which creates a backup. '
            'The achievement itself may still require an in-game trigger.'):
            return
        try:
            changes = unlock_all_attachments(root)
        except ValueError as exc:
            messagebox.showerror('Cannot unlock attachments', str(exc))
            return
        self._refresh_profile_editor()
        self.status_var.set('All 20 attachment flags enabled in memory. '
            'Click Save Encrypted Profile to save.' if changes else 'All 20 attachment flags are already enabled.')

    def _c3_attach_open_export(self):
        filename = filedialog.askopenfilename(title='Inspect exported Crysis 3 profile (read-only)',
                    filetypes=[('XML file', '*.xml'), ('All files', '*.*')])
        if not filename:
            return
        path = Path(filename)
        try:
            if path.stat().st_size > 16 * 1024 * 1024:
                raise ValueError('Exported XML is too large to inspect safely (limit 16 MB).')
            root = ET.parse(path).getroot()
            inspect_profile(root)
        except (ET.ParseError, OSError, ValueError) as exc:
            messagebox.showerror('Cannot inspect exported XML', str(exc))
            return
        self.c3_attach_external_root = root
        self.c3_attach_external_path = path
        self.c3_attach_external_active = True
        self._c3_refresh_attachments()

    def _c3_attach_use_profile(self):
        doc = getattr(self, 'profile_document', None)
        if doc is None or getattr(self, 'profile_game', '') != 'Crysis3Remastered':
            messagebox.showinfo('No game profile', 'Open an encrypted Crysis 3 Remastered profile first.')
            return
        self.c3_attach_external_active = False
        self._c3_refresh_attachments()

    def _c3_attach_export(self):
        filename = filedialog.asksaveasfilename(title='Export detected weapon attachments',
                                               defaultextension='.csv',
                                               filetypes=[('CSV', '*.csv')])
        if not filename:
            return
        try:
            export_detected_csv(Path(filename), self.c3_attach_diag)
        except OSError as exc:
            messagebox.showerror('Export failed', str(exc))
            return
        self.status_var.set(f'Exported detected weapon attachments: {filename}')
