from videotrans import translator, recognition, tts
from videotrans.configure.config import app_cfg
from videotrans.util.help_role import role_menu


class WinActionConfigMixin:

    def recogn_type_change(self):
        recogn_type = self.main.recogn_type.currentIndex()
        # Các kênh còn lại đều khai model trong cửa sổ cấu hình riêng của kênh
        self.main.model_name.setDisabled(True)
        self.main.model_name_help.setDisabled(True)

        lang = translator.get_code(show_text=self.main.source_language.currentText())

        is_allow_lang = recognition.is_allow_lang(langcode=lang, recogn_type=recogn_type,
                                                  model_name=self.main.model_name.currentText())

        self.main.show_tips.setText(str(is_allow_lang) if is_allow_lang is not True else '')

        if recognition.is_input_api(recogn_type=recogn_type) is not True:
            return

    def model_type_change(self):
        lang = translator.get_code(show_text=self.main.source_language.currentText())
        recogn_type = self.main.recogn_type.currentIndex()
        is_allow_lang = recognition.is_allow_lang(langcode=lang, recogn_type=recogn_type,
                                                  model_name=self.main.model_name.currentText())
        self.main.show_tips.setText(str(is_allow_lang) if is_allow_lang is not True else '')

    def tts_type_change(self, type):

        lang = translator.get_code(show_text=self.main.target_language.currentText())
        if lang and lang != '-':
            is_allow_lang = tts.is_allow_lang(langcode=lang, tts_type=type)            
            self.main.show_tips.setText(str(is_allow_lang) if is_allow_lang is not True else '')

        app_cfg.line_roles = {}
        _role_list = role_menu(type, lang if lang and lang != '-' else None)
        self.main.voice_role.clear()
        self.main.current_rolelist = _role_list
        self.main.voice_role.addItems(self.main.current_rolelist)
        if tts.is_input_api(tts_type=type) is not True:
            return

    def set_voice_role(self, t):
        role = self.main.voice_role.currentText()
        code = translator.get_code(show_text=t)
        if code and code != '-':
            is_allow_lang = tts.is_allow_lang(langcode=code, tts_type=self.main.tts_type.currentIndex())
            self.main.show_tips.setText(str(is_allow_lang) if is_allow_lang is not True else '')
            
            if translator.is_allow_translate(translate_type=self.main.translate_type.currentIndex(),
                                             show_target=t) is not True:
                return
        if self.main.tts_type.currentIndex() not in tts.CHANGE_BY_LANGUAGE:
            if role != 'No' and self.main.app_mode in ['biaozhun']:
                self.main.listen_btn.show()
                self.main.listen_btn.setDisabled(False)
            else:
                self.main.listen_btn.hide()
            return

        self.main.voice_role.clear()
        if t == '-' or not code:
            self.main.voice_role.addItems(['No'])
            return

        _role_list = role_menu(self.main.tts_type.currentIndex(), code.split('-')[0])
        self.main.current_rolelist = _role_list
        self.main.voice_role.addItems(_role_list)
