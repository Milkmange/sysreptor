<template>
  <full-height-page v-if="project && note" :key="project.id + note.id">
    <template #header>
      <edit-toolbar v-bind="toolbarAttrs">
        <template #title>
          <div class="note-title-container">
            <div>
              <s-btn-icon
                @click="updateKey('checked', note.checked === null ? false : !note.checked ? true : null)"
                :icon="note.checked === null ? 'mdi-checkbox-blank-off-outline' : note.checked ? 'mdi-checkbox-marked' : 'mdi-checkbox-blank-outline'"
                :disabled="readonly"
                density="comfortable"
              />
            </div>
            <s-emoji-picker-field
              v-if="note.checked === null"
              :model-value="note.icon_emoji"
              @update:model-value="updateKey('icon_emoji', $event)"
              :empty-icon="hasChildNotes ? 'mdi-folder-outline' : 'mdi-note-text-outline'"
              :readonly="readonly"
              density="comfortable"
            />
              
            <markdown-text-field-content
              id="title"
              :model-value="note.title"
              :collab="collabPropsTitle"
              @collab="notesCollab.onCollabEvent"
              @search="notesCollab.search.value = $event"
              :readonly="readonly"
              :spellcheck-supported="true"
              data-testid="note-title"
              v-bind="inputFieldAttrs"
              class="note-title"
            />
          </div>
        </template>
        <template #default>
          <s-assignee-selection
            :model-value="note.assignee"
            @update:model-value="updateKey('assignee', $event)"
            :selectable-users="project.members"
            :readonly="readonly"
            class="ml-1 mr-1"
          />

          <s-btn-icon @click="shareDialogVisible = true">
            <v-icon icon="mdi-share-variant" />
            <s-tooltip activator="parent" text="Share" />
          </s-btn-icon>

          <btn-history v-model="historyVisible" />
        </template>

        <template #context-menu>
          <btn-copy 
            :copy="performCopy"
            :readonly="notesCollab.readonly.value"
          />
          <btn-export
            :export-url="exportUrl"
            :name="'notes-' + note.title"
          />
          <btn-export
            :export-url="exportPdfUrl"
            :name="note.title"
            extension=".pdf"
            button-text="Export as PDF"
          />
        </template>
      </edit-toolbar>
    </template>
    <template #default>
      <history-timeline-project
        v-model="historyVisible"
        :project="project"
        :note="note"
        :current-url="route.fullPath"
      />

      <notes-share-dialog
        v-model="shareDialogVisible"
        :project="project"
        :note="note"
      />

      <markdown-page
        id="text"
        :model-value="note.text"
        :collab="collabPropsText"
        @collab="notesCollab.onCollabEvent"
        @search="notesCollab.search.value = $event"
        :readonly="readonly"
        data-testid="note-text"
        v-bind="inputFieldAttrs"
      />
    </template>
  </full-height-page>
</template>

<script setup lang="ts">
import { collabSubpath, type CollabPropType } from '#imports';
import { urlJoin } from "@base/utils/helpers";

const route = useRoute();
const localSettings = useLocalSettings();
const projectStore = useProjectStore();

const project = await useAsyncDataE(async () => await projectStore.getById(route.params.projectId as string));

const notesCollab = projectStore.useNotesCollab({ project: project.value, noteId: route.params.noteId as string });
const note = computed(() => notesCollab.data.value.notes[route.params.noteId as string]);
const readonly = computed(() => notesCollab.readonly.value);
const collabPropsTitle = computed<CollabPropType>((oldValue) => collabSubpath(notesCollab.collabProps.value, 'title', oldValue));
const collabPropsText = computed<CollabPropType>((oldValue) => collabSubpath(notesCollab.collabProps.value, 'text', oldValue));

const { inputFieldAttrs, errorMessage } = useProjectEditBase({
  project: computed(() => project.value),
  canUploadFiles: true,
  noReferenceItems: true,
  spellcheckEnabled: computed({ get: () => localSettings.projectNoteSpellcheckEnabled, set: (val) => { localSettings.projectNoteSpellcheckEnabled = val } }),
  markdownEditorMode: computed({ get: () => localSettings.projectNoteMarkdownEditorMode, set: (val) => { localSettings.projectNoteMarkdownEditorMode = val } }),
});
const toolbarAttrs = computed(() => ({
  data: note.value,
  errorMessage: errorMessage.value || 
    (!notesCollab.hasLock.value ? 'This note is locked by another user. Upgrade to SysReptor Professional for lock-free collaborative editing.' : null),
  delete: async (note: ProjectNote) => {
    await projectStore.deleteNote(project.value, note);
    await navigateTo(`/projects/${project.value.id}/notes/`);
  },
}));

function updateKey(key: string, value: any) {
  notesCollab.onCollabEvent({
    type: CollabEventType.UPDATE_KEY,
    path: collabSubpath(notesCollab.collabProps.value, key).path,
    value,
  })
}

const baseUrl = `/api/v1/pentestprojects/${route.params.projectId}/notes/${route.params.noteId}/`;
const historyVisible = ref(false);
const exportUrl = computed(() => urlJoin(baseUrl, '/export/'));
const exportPdfUrl = computed(() => urlJoin(baseUrl, '/export-pdf/'));
const hasChildNotes = computed(() => {
  if (!project.value || !note.value) {
    return false;
  }
  return projectStore.notes(project.value.id)
    .some(n => n.parent === note.value!.id && n.id !== note.value!.id);
});

async function performCopy() {
  if (!project.value || !note.value) {
    return;
  }
  const newNote = await projectStore.copyNote(project.value, note.value);
  await navigateTo(`/projects/${project.value.id}/notes/${newNote.id}`);
}

const shareDialogVisible = ref(false);
useAutofocus(note, 'text', (id?: string) => {
  if (id === '#shareinfo') {
    shareDialogVisible.value = true;
  }
});

</script>

<style lang="scss" scoped>
.note-title-container {
  display: flex;
  flex-direction: row;
  align-items: center;
  
  & > * {
    flex-shrink: 0;
  }

  .note-title {
    flex-grow: 1;
    flex-shrink: 1;
    min-width: 0;
    margin-left: 0.25em;
    margin-right: 0.25em;
  }
}

.assignee-container {
  width: 17em;
  min-width: 17em;
}
</style>
