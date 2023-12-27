$(function(){
    var networks = undefined;

    function showHideFormFields() {
        var security = $(this).find(':selected').attr('data-security');
        // start off with all fields hidden
        $('#identity-group').addClass('hidden');
        $('#passphrase-group').addClass('hidden');
        $('#hidden-ssid-group').addClass('hidden');
        if(security === 'NONE') {
            return; // nothing to do
        }
        if(security === 'ENTERPRISE') {
            $('#identity-group').removeClass('hidden');
            $('#passphrase-group').removeClass('hidden');
            return;
        } 
        if(security === 'HIDDEN') {
            $('#hidden-ssid-group').removeClass('hidden');
            // fall through
        } 
        // otherwise security is HIDDEN, WEP, WPA, or WPA2 which need password
        $('#passphrase-group').removeClass('hidden');
    }



    // Add an event listener to update the input field when the dropdown changes
    $('#noaa-station-dropdown').change(function () {
        var selectedOption = this.options[this.selectedIndex];
        $('#noaa-station').val(selectedOption.text);
    });



    $('#ssid-select').change(showHideFormFields);

    $.get("/regcode", function(data){
        if(data.length !== 0){
            $('#regcode').val(data);
        } else { 
            $('.reg-row').hide(); // no reg code, so hide that part of the UI
	}
    });

    $.get("/networks", function(data){
        if(data.length === 0){
            $('.before-submit').hide();
            $('#no-networks-message').removeClass('hidden');
        } else {
            networks = JSON.parse(data);
            $.each(networks, function(i, val){
                $('#ssid-select').append(
                    $('<option>')
                        .text(val.ssid)
                        .attr('val', val.ssid)
                        .attr('data-security', val.security.toUpperCase())
                );
            });

            jQuery.proxy(showHideFormFields, $('#ssid-select'))();
        }
    });

    // Function to toggle password visibility
    function togglePasswordVisibility() {
        var passwordField = $('#passphrase');
        var passwordType = passwordField.attr('type');
        passwordField.attr('type', passwordType === 'password' ? 'text' : 'password');
    }

    // Event binding for the "Show password" button
    $('#showPasswordBtn').click(togglePasswordVisibility);

    $('#connect-form').submit(function(ev){
        $.post('/connect', $('#connect-form').serialize(), function(data){
            $('.before-submit').hide();
            $('#submit-message').removeClass('hidden');
        });


        // Get the selected value from the dropdown
        var selectedStation = $('#noaa-station-dropdown').val();

        // Set the selected value to the hidden input
        $('#noaa-station').val(selectedStation);



        ev.preventDefault();
    });
});
