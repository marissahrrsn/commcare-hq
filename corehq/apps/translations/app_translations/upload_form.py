# coding=utf-8
from __future__ import absolute_import
from __future__ import unicode_literals

import copy
import six
import re
from collections import defaultdict

from django.contrib import messages
from django.utils.translation import ugettext as _
from lxml import etree
from lxml.etree import XMLSyntaxError, Element

from corehq.apps.app_manager.exceptions import XFormException
from corehq.apps.app_manager.models import ShadowForm
from corehq.apps.app_manager.util import save_xform
from corehq.apps.app_manager.xform import namespaces, WrappedNode
from corehq.apps.translations.app_translations.utils import BulkAppTranslationUpdater, get_unicode_dicts
from corehq.apps.translations.exceptions import BulkAppTranslationsException


class BulkAppTranslationFormUpdater(BulkAppTranslationUpdater):
    def __init__(self, app, identifier, lang=None):
        '''
        :param identifier: String like "menu1_form2"
        '''
        super(BulkAppTranslationFormUpdater, self).__init__(app, lang)
        self.identifier = identifier

        # These attributes depend on each other and therefore need to be created in this order
        self.form = self._get_form_from_sheet_name(self.identifier)
        self.xform = self._get_xform()
        self.itext = self._get_itext()

        # These attributes get populated by update
        self.markdowns = None
        self.markdown_vetoes = None

    def _get_form_from_sheet_name(self, sheet_name):
        mod_text, form_text = sheet_name.split("_")
        module_index = int(mod_text.replace("menu", "").replace("module", "")) - 1
        form_index = int(form_text.replace("form", "")) - 1
        return self.app.get_module(module_index).get_form(form_index)

    def _get_xform(self):
        if not isinstance(self.form, ShadowForm) and self.form.source:
            return self.form.wrapped_xform()

    def _get_itext(self):
        if self.xform:
            try:
                return self.xform.itext_node
            except XFormException:
                # Can't do anything with this form
                pass

    def update(self, rows):
        try:
            self._check_for_shadow_form_error()
        except BulkAppTranslationsException as e:
            return [(messages.error, six.text_type(e))]

        if not self.itext:
            # This form is empty or malformed. Ignore it.
            return []

        # Setup
        rows = get_unicode_dicts(rows)
        template_translation_el = self._get_template_translation_el()
        self._add_missing_translation_elements_to_itext(template_translation_el)
        self._populate_markdown_stats(rows)
        self.msgs = []

        # Skip labels that have no translation provided
        label_ids_to_skip = self._get_label_ids_to_skip(rows)

        # Update the translations
        for lang in self.langs:
            translation_node = self.itext.find("./{f}translation[@lang='%s']" % lang)
            assert(translation_node.exists())

            for row in rows:
                if row['label'] in label_ids_to_skip:
                    continue
                try:
                    self._add_or_remove_translations(lang, row)
                except BulkAppTranslationsException as e:
                    self.msgs.append((messages.warning, six.text_type(e)))

        save_xform(self.app, self.form, etree.tostring(self.xform.xml))

        return [(t, _('Error in {identifier}: ').format(identifier=self.identifier) + m) for (t, m) in self.msgs]

    def _get_template_translation_el(self):
        # Make language nodes for each language if they don't yet exist
        #
        # Currently operating under the assumption that every xForm has at least
        # one translation element, that each translation element has a text node
        # for each question and that each text node has a value node under it
        template_translation_el = None
        # Get a translation element to be used as a template for new elements
        for lang in self.langs:
            trans_el = self.itext.find("./{f}translation[@lang='%s']" % lang)
            if trans_el.exists():
                template_translation_el = trans_el
        assert(template_translation_el is not None)
        return template_translation_el

    def _add_missing_translation_elements_to_itext(self, template_translation_el):
        for lang in self.langs:
            trans_el = self.itext.find("./{f}translation[@lang='%s']" % lang)
            if not trans_el.exists():
                new_trans_el = copy.deepcopy(template_translation_el.xml)
                new_trans_el.set('lang', lang)
                if lang != self.app.langs[0]:
                    # If the language isn't the default language
                    new_trans_el.attrib.pop('default', None)
                else:
                    new_trans_el.set('default', '')
                self.itext.xml.append(new_trans_el)

    def _populate_markdown_stats(self, rows):
        # Aggregate Markdown vetoes, and translations that currently have Markdown
        self.markdowns = defaultdict(lambda: False)  # By default, Markdown is not in use
        self.markdown_vetoes = defaultdict(lambda: False)  # By default, Markdown is not vetoed for a label
        for lang in self.langs:
            # If Markdown is vetoed for one language, we apply that veto to other languages too. i.e. If a user has
            # told HQ that "**stars**" in an app's English translation is not Markdown, then we must assume that
            # "**étoiles**" in the French translation is not Markdown either.
            for row in rows:
                label_id = row['label']
                text_node = self.itext.find("./{f}translation[@lang='%s']/{f}text[@id='%s']" % (lang, label_id))
                if self._is_markdown_vetoed(text_node):
                    self.markdown_vetoes[label_id] = True
                self.markdowns[label_id] = self.markdowns[label_id] or self._had_markdown(text_node)

    def _get_label_ids_to_skip(self, rows):
        label_ids_to_skip = set()
        if self.form.is_registration_form():
            for row in rows:
                if not self._has_translation(row):
                    label_ids_to_skip.add(row['label'])
            for label in label_ids_to_skip:
                self.msgs.append((
                    messages.error,
                    _("You must provide at least one translation for the label '{}'.").format(label)))
        return label_ids_to_skip

    def _get_text_node(self, translation_node, label_id):
        text_node = translation_node.find("./{f}text[@id='%s']" % label_id)
        if text_node.exists():
            return text_node
        raise BulkAppTranslationsException(_("Unrecognized translation label {0}. "
                                             "That row has been skipped").format(label_id))

    def _add_or_remove_translations(self, lang, row):
        label_id = row['label']
        translations = self._get_translations_for_row(row, lang)
        translation_node = self.itext.find("./{f}translation[@lang='%s']" % lang)
        keep_value_node = not self.is_multi_sheet or any(v for k, v in translations.items())
        text_node = self._get_text_node(translation_node, label_id)
        for trans_type, new_translation in translations.items():
            if self.is_multi_sheet and not new_translation:
                # If the cell corresponding to the label for this question
                # in this language is empty, fall back to another language
                for l in self.langs:
                    key = self._get_col_key(trans_type, l)
                    if key not in row:
                        continue
                    fallback = row[key]
                    if fallback:
                        new_translation = fallback
                        break

            if trans_type == 'default':
                # plaintext/Markdown
                markdown_allowed = not self.markdown_vetoes[label_id] or self.markdowns[label_id]
                if self._looks_like_markdown(new_translation) and markdown_allowed:
                    # If it looks like Markdown, add it ... unless it
                    # looked like Markdown before but it wasn't. If we
                    # have a Markdown node, always keep it. FB 183536
                    self._update_translation_node(
                        new_translation,
                        text_node,
                        self._get_markdown_node(text_node),
                        {'form': 'markdown'},
                        # If all translations have been deleted, allow the
                        # Markdown node to be deleted just as we delete
                        # the plaintext node
                        delete_node=(not keep_value_node)
                    )
                self._update_translation_node(
                    new_translation,
                    text_node,
                    self._get_value_node(text_node),
                    {'form': 'default'},
                    delete_node=(not keep_value_node)
                )
            else:
                # audio/video/image
                self._update_translation_node(new_translation,
                                              text_node,
                                              text_node.find("./{f}value[@form='%s']" % trans_type),
                                              {'form': trans_type})

    def _get_translations_for_row(self, row, lang):
        translations = dict()
        for trans_type in ['default', 'image', 'audio', 'video']:
            try:
                col_key = self._get_col_key(trans_type, lang)
                translations[trans_type] = row[col_key]
            except KeyError:
                # has already been logged as unrecognized column
                pass
        return translations

    def _update_translation_node(self, new_translation, text_node, value_node, attributes=None, delete_node=True):
        if delete_node and not new_translation:
            # Remove the node if it already exists
            if value_node.exists():
                value_node.xml.getparent().remove(value_node.xml)
            return

        # Create the node if it does not already exist
        if not value_node.exists():
            e = etree.Element(
                "{f}value".format(**namespaces), attributes
            )
            text_node.xml.append(e)
            value_node = WrappedNode(e)

        # Update the translation
        value_node.xml.tail = ''
        for node in value_node.findall("./*"):
            node.xml.getparent().remove(node.xml)
        escaped_trans = self.escape_output_value(new_translation)
        value_node.xml.text = escaped_trans.text
        for n in escaped_trans.getchildren():
            value_node.xml.append(n)

    def _looks_like_markdown(self, str):
        return re.search(r'^\d+[\.\)] |^\*|~~.+~~|# |\*{1,3}\S+\*{1,3}|\[.+\]\(\S+\)', str, re.M)

    def _get_markdown_node(self, text_node_):
        return text_node_.find("./{f}value[@form='markdown']")

    def _get_value_node(self, text_node_):
        try:
            return next(
                n for n in text_node_.findall("./{f}value")
                if 'form' not in n.attrib or n.get('form') == 'default'
            )
        except StopIteration:
            return WrappedNode(None)

    def _had_markdown(self, text_node_):
        """
        Returns True if a Markdown node currently exists for a translation.
        """
        markdown_node_ = self._get_markdown_node(text_node_)
        return markdown_node_.exists()

    def _is_markdown_vetoed(self, text_node_):
        """
        Return True if the value looks like Markdown but there is no
        Markdown node. It means the user has explicitly told form
        builder that the value isn't Markdown.
        """
        value_node_ = self._get_value_node(text_node_)
        if not value_node_.exists():
            return False
        old_trans = etree.tostring(value_node_.xml, method="text", encoding="unicode").strip()
        return self._looks_like_markdown(old_trans) and not self._had_markdown(text_node_)

    def _has_translation(self, row):
        for lang_ in self.langs:
            for trans_type_ in ['default', 'image', 'audio', 'video']:
                if row.get(self._get_col_key(trans_type_, lang_)):
                    return True

    def _check_for_shadow_form_error(self):
        if isinstance(self.form, ShadowForm):
            raise BulkAppTranslationsException(_('Form {index}, "{name}", is a shadow form. '
                     'Cannot translate shadow forms, skipping.').format(index=self.form.id + 1,
                                                                        name=self.form.default_name()))

    @classmethod
    def escape_output_value(cls, value):
        try:
            return etree.fromstring("<value>{}</value>".format(
                re.sub(r"(?<!/)>", "&gt;", re.sub(r"<(\s*)(?!output)", "&lt;\\1", value))
            ))
        except XMLSyntaxError:
            # if something went horribly wrong just don't bother with escaping
            element = Element('value')
            element.text = value
            return element

    def _get_col_key(self, translation_type, lang):
        """
        Returns the name of the column in the bulk app translation spreadsheet
        given the translation type and language
        :param translation_type: What is being translated, i.e. 'default' or 'image'
        :param lang:
        """
        return "%s_%s" % (translation_type, lang)