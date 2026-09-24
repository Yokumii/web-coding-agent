<script setup>
import {ref, onMounted, onBeforeUnmount, watch} from "vue";
import {mountTreeView} from "./tree.mjs";
const props = defineProps({configure: {type: Function, required: true}});
const root = ref(null);
let component;
function mount() {
  component?.destroy();
  component = undefined;
  component = mountTreeView(props.configure(root.value));
}
onMounted(mount);
watch(() => props.configure, () => { if (root.value) mount(); }, {flush: "post"});
onBeforeUnmount(() => component?.destroy());
defineExpose({getInstance: () => component});
</script>
<template><div ref="root" /></template>
