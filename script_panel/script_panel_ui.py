__author__ = "Richard Brenick", "Niels Vaes"
                                # ^ only fuzzy search :)
import logging
# Standard
import os.path
import stat
import subprocess
import sys
from functools import partial

from script_panel import dcc
from script_panel import script_panel_settings as sps
from script_panel import script_panel_utils as spu
from script_panel.ui import command_palette
from script_panel.ui import folder_model
from script_panel.ui import snippet_popup
from script_panel.ui import ui_utils
from script_panel.ui.ui_utils import QtCore, QtWidgets, QtGui

try:
    from script_panel.dcc import script_panel_skyhook as sp_skyhook
except Exception as e:
    print("Optional skyhook import failed: {}".format(e))
    sp_skyhook = None

standalone_app = None
if not QtWidgets.QApplication.instance():
    standalone_app = QtWidgets.QApplication(sys.argv)

    from script_panel.ui import stylesheets

    stylesheets.apply_standalone_stylesheet()

PY3 = sys.version_info.major == 3

dcc_interface = dcc.DCCInterface()
folder_types = spu.FolderTypes
BACKGROUND_COLOR_FORM = "background-color:rgb({0}, {1}, {2})"
BACKGROUND_COLOR_GREEN = BACKGROUND_COLOR_FORM.format(46, 113, 46)
BACKGROUND_COLOR_RED = BACKGROUND_COLOR_FORM.format(161, 80, 55)
BACKGROUND_COLOR_WARNING_RED = BACKGROUND_COLOR_FORM.format(255, 50, 50)


class ScriptPanelWidget(QtWidgets.QWidget):
    def __init__(self, *args, **kwargs):
        super(ScriptPanelWidget, self).__init__(*args, **kwargs)

        self.ui = ScriptPanelUI()
        self.settings = sps.ScriptPanelSettings()

        self.config_data = spu.ConfigurationData()
        self.default_expand_depth = self.config_data.default_expand_depth

        # palette chooser
        self.ui.palette_chooser.addItems(self.settings.get_layout_names())
        ui_utils.set_combo_index_via_text(self.ui.palette_chooser, self.settings.active_layout)

        # build model
        self.model = QtGui.QStandardItemModel()
        self.proxy = folder_model.ScriptPanelSortProxyModel(self.model)
        self.ui.scripts_TV.setModel(self.proxy)
        self.ui.scripts_TV.setSortingEnabled(True)
        self._model_folders = {}

        # connect signals
        self.ui.search_LE.textChanged.connect(self.filter_scripts)
        self.ui.refresh_BTN.clicked.connect(self.refresh_scripts)
        self.ui.configure_BTN.clicked.connect(self.open_config_editor)
        self.ui.script_double_clicked.connect(self.script_double_clicked)
        self.ui.script_dropped_in_layout.connect(self.add_script_to_layout)
        self.ui.palette_chooser.currentIndexChanged.connect(self._palette_chooser_index_change)
        self.ui.add_palette_BTN.clicked.connect(self.add_palette_layout)
        self.ui.save_palette_BTN.clicked.connect(self.save_favorites_layout)
        self.ui.load_palette_BTN.clicked.connect(self.load_current_layout)

        # right click menus
        self.ui.scripts_TV.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.ui.scripts_TV.customContextMenuRequested.connect(self.build_context_menu)

        self.ui.command_palette_widget.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.ui.command_palette_widget.customContextMenuRequested.connect(self.build_palette_context_menu)

        # shortcuts
        self.snippet_shortcut = None
        self.register_snippet_shortcut()
        self.setup_palette_shortcuts()

        # build ui
        self.load_current_layout()
        self.refresh_scripts()
        self.load_settings()

        main_layout = QtWidgets.QVBoxLayout()
        main_layout.setContentsMargins(2, 2, 2, 2)
        main_layout.addWidget(self.ui)
        self.setLayout(main_layout)

    def setup_palette_shortcuts(self):
        del_hotkey = QtWidgets.QShortcut(
            QtGui.QKeySequence("DEL"),
            self.ui.command_palette_widget.graphics_view,
            self.ui.command_palette_widget.remove_selected_items,
        )
        del_hotkey.setContext(QtCore.Qt.WidgetShortcut)

        save_layout_hotkey = QtWidgets.QShortcut(
            QtGui.QKeySequence("Ctrl+S"),
            self.ui.command_palette_widget.graphics_view,
            self.save_favorites_layout,
        )
        save_layout_hotkey.setContext(QtCore.Qt.WidgetShortcut)

        load_layout_hotkey = QtWidgets.QShortcut(
            QtGui.QKeySequence("F5"),
            self.ui.command_palette_widget.graphics_view,
            self.load_current_layout,
        )
        load_layout_hotkey.setContext(QtCore.Qt.WidgetShortcut)

    def register_snippet_shortcut(self):
        # already registered, no need to do it again
        if self.snippet_shortcut:
            return

        snippet_key = self.config_data.get_user_data().get(spu.lk.snippet_shortcut, spu.lk.default_snippet_shortcut)

        snippet_shortcut = QtWidgets.QShortcut(
            QtGui.QKeySequence(snippet_key),
            ui_utils.get_app_window(),
            self.open_snippet_popup,
        )
        snippet_shortcut.setContext(QtCore.Qt.ApplicationShortcut)

        self.snippet_shortcut = snippet_shortcut

    def build_context_menu(self):
        selected_path = self.ui.scripts_TV.get_selected_script_paths(allow_folders=True)

        # right click menu
        script_panel_context_actions = []
        if any(selected_path):
            if os.path.isdir(selected_path[0]):
                # folder actions
                script_panel_context_actions.extend([
                    {"Add Script": self.create_script},
                    "-",
                ])
            else:
                # script actions
                script_panel_context_actions.extend([
                    {"Run": self.activate_script},
                    {"Edit": self.open_script_in_editor},
                    {"Create Hotkey / Shelf button": self.open_hotkey_editor},
                    "-",
                ])

        script_panel_context_actions.extend([
            {"RADIO_SETTING": {"settings": self.settings,
                               "settings_key": self.settings.k_double_click_action,
                               "choices": [sps.sk.run_script_on_click, sps.sk.edit_script_on_click],
                               "default": sps.sk.run_script_on_click,
                               }},
            "-",
            {"Show In Explorer": self.open_script_in_explorer},
        ])
        ui_utils.build_menu_from_action_list(script_panel_context_actions)

    def build_palette_context_menu(self):

        selected_items = self.ui.command_palette_widget.get_selected_items()
        if len(selected_items) == 1:
            selected_script_widget = selected_items[-1].wrapped_widget  # type: ScriptWidget
        else:
            selected_script_widget = None

        action_list = [
            {"Edit": self.open_favorites_script_in_editor},
            {"Remove from palette": self.ui.command_palette_widget.remove_selected_items},
            {"Hide Headers": self.ui.command_palette_widget.hide_headers},
            {"Show Headers": self.ui.command_palette_widget.show_headers},
            {"Grid": [
                {"Set Grid Size": self.ui.command_palette_widget.open_grid_size_setter},
                {"Set Grid Color": self.ui.command_palette_widget.open_grid_background_color_setter},
                "-",
                {"Reset Grid - Size": self.ui.command_palette_widget.reset_grid_size},
                {"Reset Grid - Color": self.ui.command_palette_widget.reset_grid_color},
                {"Reset Grid": self.ui.command_palette_widget.reset_grid_display},
            ]},
        ]

        if selected_script_widget:
            action_list.extend([
                "-",
                {"Set Label": selected_script_widget.open_label_editor},
                {"Set Color": selected_script_widget.open_display_color_picker},
                {"Set Icon": selected_script_widget.open_icon_browser},
                {"Set Icon - via DCC": selected_script_widget.open_dcc_icon_browser},
                {"Set Icon - Clear": selected_script_widget.clear_icon},
                {"Update Script Path": selected_script_widget.update_script_path},
                "-",
                {"Reset Display - Label": selected_script_widget.reset_display_label},
                {"Reset Display - Color": selected_script_widget.reset_display_color},
                {"Reset Display - Icon": selected_script_widget.reset_display_icon},
                {"Reset Display": selected_script_widget.reset_display},
            ])

        ui_utils.build_menu_from_action_list(action_list, extra_trigger=partial(self.ui.display_layout_save_required))

    def open_snippet_popup(self):
        snippet_data = self.config_data.user_snippets.copy()
        snippet_data.update(dcc_interface.get_default_snippets())
        snippet_popup.main(snippet_data=snippet_data)

    def load_settings(self):
        splitter_sizes = self.settings.get_value(self.settings.k_main_splitter_sizes)
        if splitter_sizes:
            splitter_sizes = [int(x) for x in splitter_sizes] # safety convert to proper type
            self.ui.main_splitter.setSizes(splitter_sizes)

        if sp_skyhook:
            skyhook_enabled = self.settings.get_value(self.settings.k_skyhook_enabled, default=False)
            self.ui.skyhook_blender_BTN.setChecked(skyhook_enabled)

    def save_settings(self):
        self.settings.setValue(self.settings.k_main_splitter_sizes, self.ui.main_splitter.sizes())
        self.settings.setValue(self.settings.k_skyhook_enabled, self.ui.skyhook_blender_BTN.isChecked())

    def config_refresh(self):
        self.config_data.refresh_config()
        self.refresh_scripts()

    def refresh_scripts(self):
        self.model.clear()
        self.model.setHorizontalHeaderLabels(["Name"])
        self._model_folders = {}

        # then add normal scripts
        for script_path, path_info in spu.get_scripts(config_data=self.config_data).items():
            self.add_script_to_model(script_path, path_info)

        self.ui.scripts_TV.expandToDepth(self.default_expand_depth)
        self.ui.scripts_TV.sortByColumn(0, QtCore.Qt.AscendingOrder)
        header = self.ui.scripts_TV.header()
        header.setSectionResizeMode(0, header.ResizeToContents)

        # run text in filter
        self.filter_scripts()

    def add_script_to_model(self, script_path, path_info):
        path_root_dir = path_info.get(spu.PathInfoKeys.root_dir)
        display_prefix = path_info.get(spu.PathInfoKeys.folder_prefix)
        root_type = path_info.get(spu.PathInfoKeys.root_type)

        # display path in tree view
        script_rel_path = os.path.relpath(script_path, path_root_dir)
        dir_rel_path = os.path.relpath(os.path.dirname(script_path), path_root_dir)
        display_dir_rel_path = dir_rel_path
        if display_prefix:
            display_dir_rel_path = "{}\\{}".format(display_prefix, display_dir_rel_path)

        root_folder_icon = icons.get_root_folder_icon_for_type(root_type)
        folder_icon = icons.get_folder_icon_for_type(root_type)
        parent_item = self.model

        # build needed folders
        folder_rel_split = display_dir_rel_path.split("\\")
        for i, token in enumerate(folder_rel_split):
            if token in [".", ""]:
                continue

            # combine together the token into a relative_path
            token_rel_display_path = "\\".join(folder_rel_split[:i + 1])
            token_rel_real_path = "\\".join(folder_rel_split[1:i + 1]) if display_prefix else token_rel_display_path
            token_full_path = os.path.join(path_root_dir, token_rel_real_path)

            # an Item for this folder has already been created
            existing_folder_item = self._model_folders.get(token_rel_display_path)
            if existing_folder_item is not None:
                parent_item = existing_folder_item
            else:
                new_folder_item = QtGui.QStandardItem(str(token))

                # set special icon if this is the root folder
                new_folder_item.setIcon(root_folder_icon) if i == 0 else new_folder_item.setIcon(folder_icon)

                # mark as folder for sorting model
                folder_path_data = folder_model.PathData(relative_path=token_rel_real_path,
                                                         full_path=token_full_path,
                                                         is_folder=True,
                                                         root_type=root_type,
                                                         )
                new_folder_item.setData(folder_path_data, QtCore.Qt.UserRole)

                parent_item.appendRow(new_folder_item)
                parent_item = new_folder_item
                self._model_folders[token_rel_display_path] = new_folder_item

        item = ScriptModelItem(script_path)
        path_data = folder_model.PathData(relative_path=script_rel_path,
                                          full_path=script_path,
                                          is_folder=False,
                                          root_type=root_type,
                                          )
        item.setData(path_data, QtCore.Qt.UserRole)

        script_icon = icons.get_script_icon_for_type(script_path, root_type)
        item.setIcon(script_icon)

        parent_item.appendRow(item)

    def save_favorites_layout(self):
        current_layout = self.ui.palette_chooser.currentText()
        self.settings.update_layout(current_layout, self._get_current_layout_settings())
        self.ui.display_layout_save_required(False)
        self.save_settings()
        print("Command Palette - layout: '{}' saved".format(current_layout))

    def _get_current_layout_settings(self):
        ui_info = dict()
        ui_info[sps.sk.meta_data] = {
            "version": 2,
            "user": os.getenv("USERNAME"),
        }
        ui_info[sps.sk.palette_layout] = self.ui.command_palette_widget.get_scene_layout()
        ui_info[sps.sk.palette_display] = self.ui.command_palette_widget.get_ui_settings()
        return ui_info

    def _palette_chooser_index_change(self):
        current_layout = self.ui.palette_chooser.currentText()
        self.settings.set_active_layout(current_layout)
        self.load_layout_settings(current_layout)

    def load_current_layout(self):
        self.settings.sync()  # sync from disk
        current_layout = self.ui.palette_chooser.currentText()
        self.load_layout_settings(current_layout)

        self.ui.display_layout_save_required(False)

    def load_layout_settings(self, layout_key=None):
        self.ui.command_palette_widget.clear()
        layout_info = self.settings.get_layout(layout_key)

        layout_info = upgrade_layout_settings_to_latest(layout_info)

        palette_layout = layout_info.get(sps.sk.palette_layout, dict())
        palette_display = layout_info.get(sps.sk.palette_display, dict())

        for script_path, palette_item_info in palette_layout.items():
            self.add_script_to_layout(
                script_path=script_path,
                display_info=palette_item_info.get("display_info"),
            )

        self.ui.command_palette_widget.set_ui_settings(palette_display)
        self.ui.command_palette_widget.set_scene_layout(palette_layout)

    def open_favorites_script_in_editor(self):
        for item in self.ui.command_palette_widget.get_selected_items():  # type: command_palette.PaletteRectItem
            script_widget = item.wrapped_widget  # type: ScriptWidget
            self.open_script_in_editor(script_widget.script_path)

    def add_script_to_layout(self, script_path, display_info=None):
        script_widget = ScriptWidget(script_path)
        script_widget.script_clicked.connect(self.activate_script)
        if display_info:
            script_widget.set_display_from_info(display_info)

        script_name = os.path.basename(script_path)
        self.ui.command_palette_widget.add_widget(
            internal_id=script_path,
            display_name=script_name,
            widget=script_widget,
            pos=self.ui.command_palette_widget.get_mouse_pos(),
        )

        if not os.path.exists(script_path):
            script_widget.set_is_missing_script(True)

    def add_palette_layout(self):
        new_layout_name, ok = QtWidgets.QInputDialog.getText(
            self,
            "Palette Name",
            "Enter the name of the new palette layout",
            QtWidgets.QLineEdit.Normal,
        )
        if not ok:
            return
        self.settings.update_layout(new_layout_name)
        self.ui.palette_chooser.addItem(new_layout_name)
        self.ui.palette_chooser.setCurrentText(new_layout_name)  # will trigger a layout load of an empty thing

    def filter_scripts(self, text=None):
        if text is None:
            text = self.ui.search_LE.text()
        
        if not text:
            # reset
            self.proxy.setFilterRegExp("")
            self.ui.scripts_TV.expandToDepth(self.default_expand_depth)
            return
        
        # let's get rid of underscores
        text = text.replace(' ', '[_ ]')
        
        # this makes a pattern that allows characters to appear in a sequence, but allows for other things in between them
        # eg, "meta per", would return "bake_and_export_metahuman_performance"
        fuzzy_pattern = '.*'.join(c for c in text)
        
        search = QtCore.QRegExp(fuzzy_pattern, QtCore.Qt.CaseInsensitive, QtCore.QRegExp.RegExp)
        self.proxy.setFilterRegExp(search)
        self.ui.scripts_TV.expandAll()

    def script_double_clicked(self, script_path):
        user_setting = self.settings.get_value(self.settings.k_double_click_action, sps.sk.run_script_on_click)

        if user_setting == sps.sk.run_script_on_click:
            self.activate_script(script_path)
        else:
            self.open_script_in_editor(script_path)

    def activate_script(self, script_path=None):
        if not script_path:
            script_path = self.get_selected_script_path()
            if not script_path:
                return

        if sp_skyhook:
            if self.ui.skyhook_blender_BTN.isChecked():
                sp_skyhook.run_script_in_blender(script_path)
                return

        spu.file_triggered(script_path)

    def open_script_in_editor(self, script_path=None):
        if not script_path:
            script_data = self.get_selected_script_data(allow_folders=False)  # type: folder_model.PathData
            script_path = script_data.full_path
            if not script_path:
                return

            # open file for edit in p4
            if script_data.root_type == folder_types.perforce:
                os.chmod(script_path, stat.S_IWRITE)

                p4_edit = subprocess.Popen(["p4", "edit", script_path], cwd=os.path.dirname(script_path), shell=True)
                p4_add = subprocess.Popen(["p4", "add", script_path], cwd=os.path.dirname(script_path), shell=True)

                if PY3:
                    p4_edit.wait(timeout=5)
                    p4_add.wait(timeout=5)

        dcc_interface.open_script(script_path)

    def open_hotkey_editor(self, script_path=None):
        if not script_path:
            script_path = self.get_selected_script_path()
            if not script_path:
                return

        from .ui import hotkey_editor
        hotkey_editor.main(reload=True, script_path=script_path)

    def open_config_editor(self):
        from .ui import config_editor
        config_editor.main(parent_window=self, reload=True)

    def open_script_in_explorer(self, script_path=None):
        if not script_path:
            script_path = self.get_selected_script_path()
            if not script_path:
                return

        subprocess.Popen(r'explorer /select, "{}"'.format(script_path))

    def create_script(self):
        script_name, ok = QtWidgets.QInputDialog.getText(
            self,
            "Script Name",
            "Enter the name of the new script",
            QtWidgets.QLineEdit.Normal,
        )
        if not ok or not script_name:
            return

        # make sure we have an extension on this script
        if "." not in script_name:
            script_name = "{}.py".format(script_name)

        folder_data = self.get_selected_script_data(allow_folders=True)
        folder_path = folder_data.full_path

        script_path = os.path.join(folder_path, script_name)
        if os.path.exists(script_path):
            logging.warning("File already exists, opening: {}".format(script_path))
            self.open_script_in_editor(script_path)
            return

        with open(script_path, "w+"):
            pass

        if folder_data.root_type == folder_types.perforce:
            subprocess.Popen(["p4", "add", script_path], cwd=os.path.dirname(script_path), shell=True)

        self.open_script_in_editor(script_path)
        self.refresh_scripts()

    def get_selected_script_path(self):
        selected_script_paths = self.ui.scripts_TV.get_selected_script_paths(allow_folders=True)
        if not selected_script_paths:
            return
        script_path = selected_script_paths[0]

        if not os.path.exists(script_path):
            show_warning_path_does_not_exist(script_path)
            return

        return script_path

    def get_selected_script_data(self, allow_folders=True):
        selected_scripts_data = self.ui.scripts_TV.get_selected_scripts_data(allow_folders=allow_folders)
        if not selected_scripts_data:
            return
        return selected_scripts_data[0]  # type: folder_model.PathData


class ScriptWidget(QtWidgets.QWidget):
    script_clicked = QtCore.Signal(str)

    def __init__(self, script_path="ExampleScript.py", *args, **kwargs):
        super(ScriptWidget, self).__init__(*args, **kwargs)

        self.palette_id = script_path  # very important for saving and loading layouts

        self.script_path = script_path
        self.script_name = os.path.basename(script_path)
        self.display_color = None
        self.icon_path = None
        self.display_label = self.script_name

        self.trigger_btn = ui_utils.ScaledContentPushButton(parent=self)
        self.trigger_btn.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Ignored)
        self.trigger_btn.setText(self.display_label)
        self.trigger_btn.clicked.connect(self.activate_script)
        self.trigger_btn.setToolTip(self.script_name)

        self.default_icon = icons.get_script_icon_for_type(script_path)
        self.trigger_btn.setIcon(self.default_icon)

        main_layout = QtWidgets.QHBoxLayout()
        main_layout.addWidget(self.trigger_btn)
        main_layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(main_layout)

    def activate_script(self):
        self.script_clicked.emit(self.script_path)

    def get_display_info(self):
        return {
            "label": self.display_label,
            "color": self.display_color,
            "icon_path": self.icon_path,
        }

    def set_display_from_info(self, display_info):
        self.set_display_label(display_info.get("label", self.display_label))
        self.set_display_color(display_info.get("color"))
        self.set_icon_from_path(display_info.get("icon_path"))

    def reset_display(self):
        self.set_display_from_info(dict())

    def reset_display_label(self):
        self.set_display_label(self.script_name)

    def reset_display_color(self):
        self.set_display_color(None)

    def reset_display_icon(self):
        self.set_icon_from_path(None)

    #########################################
    # Display utility functions
    def open_label_editor(self):
        current_text = self.trigger_btn.text()
        new_text, ok = QtWidgets.QInputDialog.getText(
            ui_utils.get_app_window(),
            "New Display Label",
            "Enter new display label for: {}".format(self.script_name),
            text=current_text,
        )
        if ok:
            self.set_display_label(new_text)

    def set_display_label(self, text):
        self.display_label = text
        self.trigger_btn.setText(text)
        self.trigger_btn.update_content_size()

    def update_script_path(self):
        selected_file, _ = QtWidgets.QFileDialog.getOpenFileName(
            ui_utils.get_app_window(),
            "Select new script file for {} - {}".format(self.script_name, self.display_label),
            filter="Script(*.py *.mel);;",
        )
        if selected_file:
            self.script_path = selected_file
            self.script_name = os.path.basename(selected_file)
            self.palette_id = selected_file  # very important for saving and loading layouts
            self.set_is_missing_script(False)

    def open_display_color_picker(self):
        new_color = ui_utils.open_color_picker(current_color=self.display_color,
                                               color_signal=self.update_display_color)
        if new_color:
            self.set_display_color(new_color.getRgb()[:3])
        else:
            self.update_display_color(self.display_color)

    def set_display_color(self, color):
        """Set the internal variable and apply the color on the widget"""
        self.display_color = color
        self.update_display_color(color)

    def update_display_color(self, color):
        """Change the display of the background color"""
        if not color:
            self.trigger_btn.setStyleSheet("")
            return

        if isinstance(color, QtGui.QColor):
            color = color.getRgb()[:3]

        self.trigger_btn.setStyleSheet(BACKGROUND_COLOR_FORM.format(*color))

    def open_icon_browser(self):
        selected_file, _ = QtWidgets.QFileDialog.getOpenFileName(
            ui_utils.get_app_window(),
            "Select icon",
            filter="Image Files (*.bmp *.cur *.gif *.icns *.ico *.jpeg *.jpg *.pbm *.pgm *.png *.ppm *.svg *.svgz *.tga *.tif *.tiff *.wbmp *.webp *.xbm *.xpm);;",
        )
        if selected_file:
            self.set_icon_from_path(selected_file)

    def open_dcc_icon_browser(self):
        dcc_icon = dcc_interface.get_dcc_icon_from_browser()
        if dcc_icon:
            self.set_icon_from_path(dcc_icon)

    def clear_icon(self):
        self.set_icon_from_path("EMPTY")

    def set_icon_from_path(self, icon_path):
        if icon_path == "EMPTY":
            self.icon_path = icon_path
            self.trigger_btn.setIcon(QtGui.QIcon())
            return

        if not icon_path:
            self.icon_path = icon_path
            self.trigger_btn.setIcon(self.default_icon)
            return

        try:
            q_icon = ui_utils.create_qicon(icon_path)
            if q_icon:
                self.trigger_btn.setIcon(q_icon)
                self.icon_path = icon_path
        except Exception as e:
            logging.warning("Unable to set icon: ", e)

    def set_is_missing_script(self, missing_script):
        if missing_script:
            self.trigger_btn.setText(self.display_label + "\nMISSING SCRIPT PATH")
            self.trigger_btn.setStyleSheet(BACKGROUND_COLOR_WARNING_RED)
        else:
            self.trigger_btn.setText(self.display_label)
            self.set_display_color(self.display_color)

        self.trigger_btn.update_content_size()


class ScriptModelItem(QtGui.QStandardItem):
    def __init__(self, script_path=None):
        super(ScriptModelItem, self).__init__()
        self.script_path = script_path.replace("/", "\\")
        self.script_name = os.path.basename(script_path)
        self.setData(self.script_name, QtCore.Qt.DisplayRole)
        self.setFlags(self.flags() ^ QtCore.Qt.ItemIsDropEnabled)


###################################
# General UI

class Icons(object):
    def __init__(self):
        self.script_panel_icon = ui_utils.create_qicon("script_panel_icon")

        self.python_icon = ui_utils.create_qicon("python_icon")
        self.mel_icon = ui_utils.create_qicon("mel_icon")
        self.unknown_type_icon = ui_utils.create_qicon("unknown_icon")
        self.folder_icon = ui_utils.create_qicon("folder_icon")

        self.network_folder_icon = ui_utils.create_qicon("network_folder_icon")

        self.p4_icon = ui_utils.create_qicon("p4_icon")
        self.p4_folder_icon = ui_utils.create_qicon("p4_folder_icon")
        self.p4_python_icon = ui_utils.create_qicon("p4_python_icon")

    def get_folder_icon_for_type(self, folder_type):
        """Get Icon for normal folders"""
        if folder_type == folder_types.local or folder_type == folder_types.network:
            return self.folder_icon
        if folder_type == folder_types.perforce:
            return self.p4_folder_icon

        return self.unknown_type_icon

    def get_root_folder_icon_for_type(self, folder_type):
        """Get Icon to display as the root folder"""
        if folder_type == folder_types.local:
            return self.folder_icon
        if folder_type == folder_types.network:
            return self.network_folder_icon
        if folder_type == folder_types.perforce:
            return self.p4_icon

        return self.unknown_type_icon

    def get_script_icon_for_type(self, file_name, folder_type=""):
        """get icon for the script"""
        if file_name.endswith(".py"):
            if folder_type == folder_types.perforce:
                return self.p4_python_icon
            return self.python_icon

        elif file_name.endswith(".mel"):
            return self.mel_icon

        return self.unknown_type_icon


icons = Icons()


class ScriptPanelWindow(ui_utils.ToolWindow):
    def __init__(self, *args, **kwargs):
        super(ScriptPanelWindow, self).__init__(*args, **kwargs)

        self.main_widget = ScriptPanelWidget()
        self.setCentralWidget(self.main_widget)
        self.setWindowTitle("Script Panel")
        self.setWindowIcon(icons.script_panel_icon)
        self.resize(1000, 1000)

    def on_close(self):
        self.main_widget.save_settings()

    def closeEvent(self, event):
        self.main_widget.save_settings()
        super(ScriptPanelWindow, self).closeEvent(event)


class ScriptPanelUI(QtWidgets.QWidget):
    script_double_clicked = QtCore.Signal(str)
    script_dropped_in_layout = QtCore.Signal(str)

    def __init__(self, *args, **kwargs):
        super(ScriptPanelUI, self).__init__(*args, **kwargs)

        main_layout = QtWidgets.QVBoxLayout()
        main_layout.setSpacing(2)
        main_layout.setContentsMargins(0, 0, 0, 0)

        self.search_LE = QtWidgets.QLineEdit()
        self.search_LE.setClearButtonEnabled(True)
        self.search_LE.setPlaceholderText("Fuzzy search...")
        self.search_LE.setSizePolicy(QtWidgets.QSizePolicy.MinimumExpanding, QtWidgets.QSizePolicy.Minimum)

        self.refresh_BTN = QtWidgets.QPushButton()
        self.refresh_BTN.setIcon(ui_utils.create_qicon("refresh_icon"))
        self.refresh_BTN.setToolTip("Refresh script(s) folder(s)")

        self.configure_BTN = QtWidgets.QPushButton()
        self.configure_BTN.setIcon(ui_utils.create_qicon("settings_icon"))
        self.configure_BTN.setToolTip("Configure Script Panel")

        if sp_skyhook:
            self.skyhook_blender_BTN = QtWidgets.QPushButton()
            self.skyhook_blender_BTN.setIcon(ui_utils.create_qicon("blender_icon"))
            self.skyhook_blender_BTN.setToolTip("Check this to activate the scripts in Blender via Skyhook")
            self.skyhook_blender_BTN.setCheckable(True)

        self.palette_chooser = QtWidgets.QComboBox()
        self.save_palette_BTN = QtWidgets.QPushButton(text="Save")
        self.load_palette_BTN = QtWidgets.QPushButton(text="Load")
        self.add_palette_BTN = QtWidgets.QPushButton(text="+")
        self.palette_chooser.setSizePolicy(QtWidgets.QSizePolicy.MinimumExpanding, QtWidgets.QSizePolicy.Minimum)
        self.command_palette_widget = command_palette.CommandPaletteWidget()
        self.command_palette_widget.graphics_view.item_dropped.connect(self.palette_item_dropped)

        self.scripts_TV = ScriptTreeView()
        self.scripts_TV.setSelectionMode(QtWidgets.QListView.ExtendedSelection)
        self.scripts_TV.setAlternatingRowColors(True)
        self.scripts_TV.setDragEnabled(True)
        self.scripts_TV.setDefaultDropAction(QtCore.Qt.IgnoreAction)
        self.scripts_TV.setDragDropOverwriteMode(False)
        self.scripts_TV.setDragDropMode(QtWidgets.QAbstractItemView.DragOnly)
        self.scripts_TV.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.scripts_TV.doubleClicked.connect(self.action_script_double_clicked)

        palette_buttons_layout = QtWidgets.QHBoxLayout()
        palette_buttons_layout.addWidget(self.palette_chooser)
        palette_buttons_layout.addWidget(self.add_palette_BTN)
        palette_buttons_layout.addWidget(self.save_palette_BTN)
        palette_buttons_layout.addWidget(self.load_palette_BTN)
        palette_layout = QtWidgets.QVBoxLayout()
        palette_layout.addLayout(palette_buttons_layout)
        palette_layout.addWidget(self.command_palette_widget)
        palette_layout.setContentsMargins(0, 0, 0, 0)
        palette_layout.setSpacing(2)
        palette_widget = QtWidgets.QWidget()
        palette_widget.setLayout(palette_layout)

        scripts_and_search_layout = QtWidgets.QVBoxLayout()
        search_bar_layout = QtWidgets.QHBoxLayout()
        search_bar_layout.addWidget(self.search_LE)
        search_bar_layout.addWidget(self.refresh_BTN)
        search_bar_layout.addWidget(self.configure_BTN)
        if sp_skyhook:
            search_bar_layout.addWidget(self.skyhook_blender_BTN)

        scripts_and_search_layout.addLayout(search_bar_layout)
        scripts_and_search_layout.addWidget(self.scripts_TV)
        scripts_and_search_layout.setSpacing(2)
        scripts_and_search_layout.setContentsMargins(0, 0, 0, 0)
        scripts_and_search_widget = QtWidgets.QWidget()
        scripts_and_search_widget.setLayout(scripts_and_search_layout)

        self.main_splitter = QtWidgets.QSplitter()
        self.main_splitter.setOrientation(QtCore.Qt.Orientation.Vertical)
        self.main_splitter.setHandleWidth(10)
        self.main_splitter.addWidget(palette_widget)
        self.main_splitter.addWidget(scripts_and_search_widget)

        main_layout.addWidget(self.main_splitter)
        self.setLayout(main_layout)

        self.display_layout_save_required(False)

    def action_script_double_clicked(self, index):
        proxy = self.scripts_TV.model()  # type: QtCore.QSortFilterProxyModel
        model_index = proxy.mapToSource(index)
        script_item = proxy.sourceModel().itemFromIndex(model_index)  # type: ScriptModelItem

        if isinstance(script_item, ScriptModelItem):
            self.script_double_clicked.emit(script_item.script_path)

    def palette_item_dropped(self, event):
        """
        :type event: QtGui.QDropEvent
        """
        if isinstance(event.source(), ScriptTreeView):
            selected_scripts = self.scripts_TV.get_selected_script_paths()
            if selected_scripts:
                self.script_dropped_in_layout.emit(selected_scripts[0])
                self.display_layout_save_required()

    def display_layout_save_required(self, needs_save=True):
        if needs_save:
            self.save_palette_BTN.setStyleSheet(BACKGROUND_COLOR_RED)
        else:
            self.save_palette_BTN.setStyleSheet(BACKGROUND_COLOR_GREEN)


# class FavoritesTextOverlay(QtWidgets.QWidget):
#     def __init__(self, parent=None):
#         super(FavoritesTextOverlay, self).__init__(parent)
#
#         palette = QtGui.QPalette(self.palette())
#         palette.setColor(palette.Background, QtCore.Qt.transparent)
#
#         self.font_size = 13
#         self.empty_list_message = "Drag and drop a script here to favorite it"
#
#         self.setPalette(palette)
#
#     def paintEvent(self, event):
#         painter = QtGui.QPainter()
#         painter.begin(self)
#         painter.setRenderHint(QtGui.QPainter.Antialiasing)
#         painter.fillRect(event.rect(), QtGui.QBrush(QtGui.QColor(100, 100, 100, 100)))
#         painter.setFont(QtGui.QFont("seqoe", self.font_size))
#         painter.drawText(event.rect(), QtCore.Qt.AlignCenter, self.empty_list_message)
#         painter.setPen(QtGui.QPen(QtCore.Qt.NoPen))


class ScriptTreeView(QtWidgets.QTreeView):
    def __init__(self, *args, **kwargs):
        super(ScriptTreeView, self).__init__(*args, **kwargs)

    def get_selected_script_paths(self, allow_folders=False):
        proxy = self.model()  # type: QtCore.QSortFilterProxyModel

        selected_paths = []
        for index in self.selectedIndexes():
            model_index = proxy.mapToSource(index)
            path_data = proxy.sourceModel().data(model_index, QtCore.Qt.UserRole)  # type: folder_model.PathData

            selected_path = path_data.full_path

            # skip folders if they're not allowed
            if allow_folders is False and path_data.is_folder:
                selected_path = None

            if selected_path:
                selected_paths.append(selected_path)

        return selected_paths

    def get_selected_scripts_data(self, allow_folders=False):
        proxy = self.model()  # type: QtCore.QSortFilterProxyModel

        selected_data = []
        for index in self.selectedIndexes():
            model_index = proxy.mapToSource(index)
            path_data = proxy.sourceModel().data(model_index, QtCore.Qt.UserRole)  # type: folder_model.PathData

            # skip folders if they're not allowed
            if allow_folders is False and path_data.is_folder:
                path_data = None

            if path_data:
                selected_data.append(path_data)

        return selected_data


def show_warning_path_does_not_exist(file_path):
    """
    Show a prompt when a script file does not exist anywhere on disk
    """
    msgbox = QtWidgets.QMessageBox(ui_utils.get_app_window())
    msgbox.setIcon(QtWidgets.QMessageBox.Warning)
    msgbox.setWindowTitle("File does not exist")
    msgbox.setText("File could not be found at this location: \n{}".format(file_path))

    msgbox.setStandardButtons(QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel)
    yes_button = msgbox.button(QtWidgets.QMessageBox.Yes)
    yes_button.setText("Open Folder")
    msgbox.exec_()

    if msgbox.clickedButton() == yes_button:
        folder_path = spu.get_existing_folder(file_path)

        if not folder_path:
            sys.stdout.write("No existing folder could be found anywhere from path: {}".format(file_path))
            return

        subprocess.Popen(r'explorer "{}"'.format(folder_path))


def upgrade_layout_settings_to_latest(layout_info):
    layout_metadata = layout_info.get(sps.sk.meta_data, {})

    if layout_metadata.get("version") == 0:
        # combine scripts_display and palette_layout
        scripts_display = layout_info.get(sps.sk.scripts_display)
        palette_layout = layout_info.get(sps.sk.palette_layout, dict())

        path_file_mapping = {}
        for script_path, display_info in scripts_display.items():
            path_file_mapping[os.path.basename(script_path)] = script_path

        upgraded_palette_layout = {}
        for script_name, palette_item_info in palette_layout.items():
            script_path = path_file_mapping.get(script_name)
            palette_item_info["display_info"] = scripts_display.get(script_path)
            upgraded_palette_layout[script_path] = palette_item_info

        layout_info[sps.sk.palette_layout] = upgraded_palette_layout

    return layout_info


def main(reload=False):
    win = ScriptPanelWindow()
    win.main(reload=reload)

    if standalone_app:
        ui_utils.standalone_app_window = win
        sys.exit(standalone_app.exec_())

    return win


if __name__ == '__main__':
    main()
